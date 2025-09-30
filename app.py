import os
import json
import time
import sqlite3
import requests
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from dataclasses import dataclass, asdict
import numpy as np
import logging

# Configurazione logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configurazione diretta con URL completi (più robusta per Balena)
BLYNK_TOKEN = os.environ.get('BLYNK_TOKEN', '_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73') # portello
BLYNK_SERVER = os.environ.get('BLYNK_SERVER', 'fra1.blynk.cloud')
SAMPLING_INTERVAL = int(os.environ.get('SAMPLING_INTERVAL', '30'))
TEST_DURATION_HOURS = int(os.environ.get('TEST_DURATION_HOURS', '336')) # 2 settimane
DEBUG_MODE = os.environ.get('DEBUG_MODE', 'true').lower() == 'true'

# Mappa URL completi per bypass problemi variabili ambiente
BLYNK_URLS = {
    'pressure': f"https://fra1.blynk.cloud/external/api/get?token=_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v19",
    'flow': f"https://fra1.blynk.cloud/external/api/get?token=_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v10",
    'pwm': f"https://fra1.blynk.cloud/external/api/get?token=_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v26",
    'temperature': f"https://fra1.blynk.cloud/external/api/get?token=_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v8",
    'pm_value': f"https://fra1.blynk.cloud/external/api/get?token=_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v4"
}

# Fallback: se le variabili ambiente sono disponibili, ricostruisci URL dinamicamente
if BLYNK_TOKEN and BLYNK_TOKEN != '_PtiUhnhKwhtkmhsVz8G76bWCw3Uzs73&v19' and BLYNK_SERVER:
    BLYNK_URLS = {
        'pressure': f"https://{BLYNK_SERVER}/external/api/get?token={BLYNK_TOKEN}&v19",
        'flow': f"https://{BLYNK_SERVER}/external/api/get?token={BLYNK_TOKEN}&v10",
        'pwm': f"https://{BLYNK_SERVER}/external/api/get?token={BLYNK_TOKEN}&v26",
        'temperature': f"https://{BLYNK_SERVER}/external/api/get?token={BLYNK_TOKEN}&v8",
        'pm_value': f"https://{BLYNK_SERVER}/external/api/get?token={BLYNK_TOKEN}&v4"
    }

@dataclass
class SystemData:
    timestamp: str
    pwm_percentage: float
    pressure_measured: float
    flow_blynk: float  # Flusso da Blynk per confronto
    temperature: float
    pm_value: float

@dataclass
class CalculatedMetrics:
    pressure_clean: float
    obstruction_index: float
    filter_wear_percent: float
    filter_efficiency: float
    obstruction_trend: float
    hours_since_change: float
    filter_change_needed: bool
    system_anomaly_detected: bool
    predicted_hours_remaining: float
    flow_calculated: float  # Flusso calcolato dal nostro algoritmo

class BlynkDirectClient:
    """Client Blynk con URL diretti per massima affidabilità"""
    
    def __init__(self, url_mapping: dict):
        self.urls = url_mapping
        self.session = requests.Session()
        self.session.timeout = 15
        
        # Headers per migliorare compatibilità
        self.session.headers.update({
            'User-Agent': 'BalenaIoT-PredictiveMaintenance/1.0',
            'Accept': 'application/json'
        })
        
        logger.info("Blynk Direct Client inizializzato")
        for name, url in self.urls.items():
            # Nascondi token nei log per sicurezza
            safe_url = url.replace(url.split('token=')[1].split('&')[0], 'TOKEN_HIDDEN')
            logger.info(f"  {name}: {safe_url}")
        
    def get_pin_value(self, pin_name: str) -> float:
        """Ottieni valore da URL diretto"""
        try:
            if pin_name not in self.urls:
                logger.error(f"Pin {pin_name} non configurato")
                return 0.0
                
            url = self.urls[pin_name]
            logger.debug(f"GET: {pin_name}")
            
            response = self.session.get(url)
            response.raise_for_status()
            
            # Parse risposta Blynk
            data = response.json()
            
            if isinstance(data, list):
                value = float(data[0]) if data and len(data) > 0 else 0.0
            elif isinstance(data, (int, float)):
                value = float(data)
            elif isinstance(data, str):
                try:
                    value = float(data)
                except ValueError:
                    logger.warning(f"Valore non numerico da {pin_name}: {data}")
                    value = 0.0
            else:
                logger.warning(f"Formato risposta sconosciuto da {pin_name}: {type(data)}")
                value = 0.0
                
            logger.debug(f"{pin_name}: {value}")
            return value
            
        except requests.exceptions.Timeout:
            logger.error(f"Timeout lettura {pin_name}")
            return 0.0
        except requests.exceptions.ConnectionError:
            logger.error(f"Errore connessione {pin_name}")
            return 0.0
        except requests.exceptions.HTTPError as e:
            logger.error(f"Errore HTTP {pin_name}: {e.response.status_code if e.response else 'unknown'}")
            return 0.0
        except (ValueError, TypeError) as e:
            logger.error(f"Errore parsing {pin_name}: {e}")
            return 0.0
        except Exception as e:
            logger.error(f"Errore generico {pin_name}: {e}")
            return 0.0
    
    def get_multiple_pins(self, pin_names: list) -> dict:
        """Ottieni valori multipli pin"""
        results = {}
        
        for pin_name in pin_names:
            value = self.get_pin_value(pin_name)
            results[pin_name] = value
            
        return results
    
    def test_connectivity(self) -> dict:
        """Test connettività a tutti i pin"""
        results = {}
        
        logger.info("Test connettività Blynk...")
        for pin_name in self.urls.keys():
            try:
                value = self.get_pin_value(pin_name)
                results[pin_name] = {'status': 'OK', 'value': value}
                logger.info(f"  ✓ {pin_name}: {value}")
            except Exception as e:
                results[pin_name] = {'status': 'ERROR', 'error': str(e)}
                logger.error(f"  ✗ {pin_name}: {e}")
                
        return results

class PredictiveAlgorithm:
    """Algoritmo manutenzione predittiva ottimizzato"""
    
    def __init__(self):
        # Curve ventole semplificate (punti chiave)
        self.fan_flow = np.array([0, 500, 1000, 1500, 2000, 2500, 2950])
        self.fan_pressure = np.array([450, 320, 240, 180, 120, 70, 20])
        
        # Curve filtri
        self.filter_flow = np.array([0, 850, 1700, 2125.4, 2550, 3400, 4250, 5100, 5950, 6375])  # *4 filtri
        self.filter_pressure = np.array([28, 32, 38, 42, 47, 56, 66, 78, 92, 100])
        
        # Sistema (2 ventole)
        self.fan_flow_total = self.fan_flow * 2
        
        # Parametri PWM ESP32 (corrispondenti al tuo codice)
        self.MIN_FAN_SPEED_PWM = 64   # 25% PWM fisico
        self.MAX_FAN_SPEED_PWM = 153  # 60% PWM fisico  
        
        # Parametri
        self.MAX_OBSTRUCTION_WARNING = 1.5
        self.MAX_OBSTRUCTION_CRITICAL = 2.0
        self.FILTER_CHANGE_HOURS = 2000
        
        # Stato interno
        self.hours_since_change = 0
        self.obstruction_history = [1.0] * 10
        self.history_index = 0
        
    def convert_blynk_pwm_to_real_speed(self, blynk_pwm_percent: float) -> float:
        """
        Converte la percentuale PWM da Blynk alla velocità effettiva delle ventole
        
        Il sistema funziona così:
        - PWM Range ESP32: 0-255 (8-bit)
        - Ventole si avviano a: 64 (25% del range 0-255)
        - Cap di protezione a: 153 (60% del range 0-255) 
        - Range operativo effettivo: 64-153 (25%-60% del PWM fisico)
        - Blynk pubblica: 1-100% che mappa su questo range operativo
        
        Args:
            blynk_pwm_percent: Percentuale da Blynk (0-100%)
            
        Returns:
            Percentuale velocità per scalare le curve caratteristiche
        """
        if blynk_pwm_percent == 0:
            return 0.0
            
        # Converte da percentuale Blynk (1-100%) a duty cycle PWM fisico (64-153)
        duty_cycle = self.map_value(blynk_pwm_percent, 1, 100, self.MIN_FAN_SPEED_PWM, self.MAX_FAN_SPEED_PWM)
        
        # Calcola la percentuale del PWM fisico ESP32 (0-255 range)
        physical_pwm_percent = (duty_cycle / 255.0) * 100.0  # Es: 153/255 = 60%
        
        # Per le curve caratteristiche delle ventole:
        # - Il punto di avvio (25%) corrisponde al minimo delle curve
        # - Il cap attuale (60%) corrisponde al massimo configurato, NON al 100% delle ventole
        # - Quindi usiamo il mapping 25%-60% → 0%-100% delle curve caratteristiche
        
        # Normalizza dal range operativo (25%-60%) al range curve (0%-100%)
        if physical_pwm_percent <= 25.0:
            curve_scale_percent = 0.0  # Sotto il minimo operativo
        else:
            # Scala linearmente da 25%-60% fisico a 0%-100% curve
            curve_scale_percent = self.map_value(physical_pwm_percent, 25.0, 60.0, 0.0, 100.0)
            curve_scale_percent = max(0.0, min(100.0, curve_scale_percent))
        
        return curve_scale_percent
        
    def map_value(self, x: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
        """Equivalente della funzione map() di Arduino"""
        return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min
        
    def interpolate(self, x: float, x_arr: np.ndarray, y_arr: np.ndarray) -> float:
        """Interpolazione lineare veloce"""
        if x <= x_arr[0]:
            return max(0.0, y_arr[0])
        if x >= x_arr[-1]:
            return max(0.0, y_arr[-1])
        return max(0.0, np.interp(x, x_arr, y_arr))
    
    def calculate_metrics(self, system_data: SystemData) -> CalculatedMetrics:
        """Calcola metriche principali"""
        
        # Converte PWM Blynk a velocità reale ventole
        real_fan_speed = self.convert_blynk_pwm_to_real_speed(system_data.pwm_percentage)
        
        # Scala curva ventole per velocità reale
        scale_factor = real_fan_speed / 100.0
        q_fan_scaled = self.fan_flow_total * scale_factor
        p_fan_scaled = self.fan_pressure * scale_factor * scale_factor
        
        # Calcola portata dalla pressione
        flow_calculated = self.interpolate(
            system_data.pressure_measured, 
            p_fan_scaled, 
            q_fan_scaled
        )
        
        # Pressione filtro pulito teorica
        pressure_clean = self.interpolate(
            flow_calculated,
            self.filter_flow,
            self.filter_pressure
        )
        
        # Indice ostruzione
        obstruction_index = 1.0
        if pressure_clean > 0.1:
            obstruction_index = system_data.pressure_measured / pressure_clean
            
        # Metriche derivate
        filter_wear = max(0.0, min(100.0, (obstruction_index - 1.0) * 100.0))
        filter_efficiency = max(0.0, min(100.0, (2.0 - obstruction_index) * 100.0))
        
        # Trend ostruzione
        self.obstruction_history[self.history_index] = obstruction_index
        self.history_index = (self.history_index + 1) % 10
        
        recent_avg = sum(self.obstruction_history[:5]) / 5.0
        older_avg = sum(self.obstruction_history[5:]) / 5.0
        obstruction_trend = recent_avg - older_avg
        
        # Predizioni
        filter_change_needed = bool(
            obstruction_index >= self.MAX_OBSTRUCTION_CRITICAL or
            self.hours_since_change >= self.FILTER_CHANGE_HOURS
        )
        
        system_anomaly = bool(
            system_data.pressure_measured > 500 or 
            system_data.pressure_measured < 0 or
            obstruction_index > self.MAX_OBSTRUCTION_CRITICAL
        )
        
        # Ore rimanenti stimate
        if filter_wear > 0 and self.hours_since_change > 10:
            degradation_rate = filter_wear / self.hours_since_change
            predicted_hours = (100.0 - filter_wear) / degradation_rate if degradation_rate > 0 else 999
        else:
            predicted_hours = 999
            
        return CalculatedMetrics(
            pressure_clean=pressure_clean,
            obstruction_index=obstruction_index,
            filter_wear_percent=filter_wear,
            filter_efficiency=filter_efficiency,
            obstruction_trend=obstruction_trend,
            hours_since_change=self.hours_since_change,
            filter_change_needed=filter_change_needed,
            system_anomaly_detected=system_anomaly,
            predicted_hours_remaining=min(predicted_hours, 999),
            flow_calculated=flow_calculated
        )

class TestDatabase:
    """Database SQLite ottimizzato per Balena"""
    
    def __init__(self, db_path: str = "/data/test_data.db"):
        self.db_path = db_path
        # Crea directory se non esiste
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.init_database()
        
    def init_database(self):
        """Inizializza database"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS test_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                pwm_percentage REAL,
                pressure_measured REAL,
                flow_blynk REAL,
                flow_calculated REAL,
                temperature REAL,
                pm_value REAL,
                pressure_clean REAL,
                obstruction_index REAL,
                filter_wear_percent REAL,
                filter_efficiency REAL,
                obstruction_trend REAL,
                hours_since_change REAL,
                filter_change_needed INTEGER,
                system_anomaly_detected INTEGER,
                predicted_hours_remaining REAL
            )
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                event_type TEXT,
                message TEXT,
                severity TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        
    def save_data_point(self, system_data: SystemData, metrics: CalculatedMetrics):
        """Salva singolo punto dati"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO test_data 
            (timestamp, pwm_percentage, pressure_measured, flow_blynk, flow_calculated,
             temperature, pm_value, pressure_clean, obstruction_index, filter_wear_percent,
             filter_efficiency, obstruction_trend, hours_since_change, filter_change_needed,
             system_anomaly_detected, predicted_hours_remaining)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            system_data.timestamp, system_data.pwm_percentage, system_data.pressure_measured,
            system_data.flow_blynk, metrics.flow_calculated, system_data.temperature,
            system_data.pm_value, metrics.pressure_clean, metrics.obstruction_index,
            metrics.filter_wear_percent, metrics.filter_efficiency, metrics.obstruction_trend,
            metrics.hours_since_change, 1 if metrics.filter_change_needed else 0,
            1 if metrics.system_anomaly_detected else 0, metrics.predicted_hours_remaining
        ))
        conn.commit()
        conn.close()
        
    def get_recent_data(self, hours: int = 24) -> list:
        """Ottieni dati recenti per dashboard"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT * FROM test_data 
            WHERE datetime(timestamp) > datetime('now', '-{} hours')
            ORDER BY timestamp DESC
            LIMIT 1000
        '''.format(hours))
        
        data = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return data
    
    def get_statistics(self) -> dict:
        """Statistiche generali"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute('''
            SELECT 
                COUNT(*) as total_points,
                MAX(filter_wear_percent) as max_wear,
                MAX(obstruction_index) as max_obstruction,
                SUM(filter_change_needed) as change_alerts,
                SUM(system_anomaly_detected) as anomalies,
                MIN(timestamp) as start_time,
                MAX(timestamp) as end_time
            FROM test_data
        ''')
        
        stats = dict(cursor.fetchone())
        conn.close()
        return stats

# Istanze globali con client corretto
blynk_client = BlynkDirectClient(BLYNK_URLS)
algorithm = PredictiveAlgorithm()
database = TestDatabase()

# Flask app
app = Flask(__name__)

# Variabili stato globali
test_running = False
test_stats = {"start_time": None, "data_points": 0, "last_update": None}

def data_collection_loop():
    """Loop principale raccolta dati"""
    global test_running, test_stats
    
    logger.info("Avvio raccolta dati...")
    test_running = True
    test_stats["start_time"] = datetime.now().isoformat()
    
    while test_running:
        try:
            # Ottieni dati da Blynk con nuovo client
            blynk_data = blynk_client.get_multiple_pins(['pressure', 'flow', 'pwm', 'temperature', 'pm_value'])
            
            # Crea oggetto dati sistema
            system_data = SystemData(
                timestamp=datetime.now().isoformat(),
                pwm_percentage=blynk_data.get('pwm', 0),
                pressure_measured=blynk_data.get('pressure', 0),
                flow_blynk=blynk_data.get('flow', 0),
                temperature=blynk_data.get('temperature', 20),
                pm_value=blynk_data.get('pm_value', 0)
            )
            
            # Calcola metriche
            metrics = algorithm.calculate_metrics(system_data)
            
            # Salva in database
            database.save_data_point(system_data, metrics)
            
            # Aggiorna statistiche
            test_stats["data_points"] += 1
            test_stats["last_update"] = datetime.now().isoformat()
            algorithm.hours_since_change += SAMPLING_INTERVAL / 3600
            
            # Log eventi importanti
            if metrics.filter_change_needed:
                logger.warning(f"ALERT: Cambio filtro necessario - Usura: {metrics.filter_wear_percent:.1f}%")
                
            if metrics.system_anomaly_detected:
                logger.error(f"ANOMALY: Anomalia sistema - Ostruzione: {metrics.obstruction_index:.2f}")
            
            if DEBUG_MODE:
                real_speed = algorithm.convert_blynk_pwm_to_real_speed(system_data.pwm_percentage)
                physical_pwm = ((algorithm.map_value(system_data.pwm_percentage, 1, 100, 64, 153) / 255.0) * 100) if system_data.pwm_percentage > 0 else 0
                logger.info(f"PWM_Blynk: {system_data.pwm_percentage:.0f}% | "
                           f"PWM_Physical: {physical_pwm:.1f}% | " 
                           f"Curve_Scale: {real_speed:.1f}% | "
                           f"P: {system_data.pressure_measured:.1f}Pa | "
                           f"Q_calc: {metrics.flow_calculated:.0f}m³/h | "
                           f"Q_blynk: {system_data.flow_blynk:.0f}m³/h | "
                           f"Wear: {metrics.filter_wear_percent:.1f}%")
            
        except Exception as e:
            logger.error(f"Errore raccolta dati: {e}")
        
        time.sleep(SAMPLING_INTERVAL)
    
    logger.info("Raccolta dati terminata")

# Routes Flask per dashboard web
@app.route('/')
def dashboard():
    """Dashboard principale"""
    return render_template('dashboard.html')

def convert_numpy_types(obj):
    """Converte tipi numpy in tipi Python nativi per JSON"""
    if isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, (np.int_, np.intc, np.intp, np.int8, np.int16, np.int32, np.int64)):
        return int(obj)
    elif isinstance(obj, (np.float_, np.float16, np.float32, np.float64)):
        return float(obj)
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    return obj

@app.route('/api/current')
def api_current():
    """API dati attuali"""
    try:
        # Ottieni dati Blynk con nuovo client
        blynk_data = blynk_client.get_multiple_pins(['pressure', 'flow', 'pwm', 'temperature', 'pm_value'])
        
        system_data = SystemData(
            timestamp=datetime.now().isoformat(),
            pwm_percentage=float(blynk_data.get('pwm', 0)),
            pressure_measured=float(blynk_data.get('pressure', 0)),
            flow_blynk=float(blynk_data.get('flow', 0)),
            temperature=float(blynk_data.get('temperature', 20)),
            pm_value=float(blynk_data.get('pm_value', 0))
        )
        
        metrics = algorithm.calculate_metrics(system_data)
        
        # Converte tutti i dati in tipi serializzabili
        response_data = {
            'system_data': convert_numpy_types(asdict(system_data)),
            'metrics': convert_numpy_types(asdict(metrics)),
            'test_stats': convert_numpy_types(test_stats),
            'status': 'running' if test_running else 'stopped'
        }
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Errore API current: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/history/<int:hours>')
def api_history(hours):
    """API dati storici"""
    try:
        data = database.get_recent_data(hours)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/statistics')
def api_statistics():
    """API statistiche"""
    try:
        stats = database.get_statistics()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/control', methods=['POST'])
def api_control():
    """Controllo test"""
    global test_running
    
    action = request.json.get('action')
    
    if action == 'start' and not test_running:
        # Avvia thread raccolta dati
        thread = threading.Thread(target=data_collection_loop, daemon=True)
        thread.start()
        return jsonify({'status': 'started'})
        
    elif action == 'stop':
        test_running = False
        return jsonify({'status': 'stopped'})
        
    elif action == 'reset_filter':
        algorithm.hours_since_change = 0
        algorithm.obstruction_history = [1.0] * 10
        logger.info("Timer filtro resettato")
        return jsonify({'status': 'filter_reset'})
        
    return jsonify({'error': 'Invalid action'}), 400

@app.route('/api/export')
def api_export():
    """Export dati CSV"""
    try:
        import io
        from flask import make_response
        
        data = database.get_recent_data(24 * 7)  # 1 settimana
        
        if not data:
            return jsonify({'error': 'No data available'}), 404
            
        # Crea CSV
        output = io.StringIO()
        if data:
            # Header
            headers = list(data[0].keys())
            output.write(','.join(headers) + '\n')
            
            # Data rows  
            for row in data:
                values = [str(row[h]) for h in headers]
                output.write(','.join(values) + '\n')
        
        response = make_response(output.getvalue())
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = f'attachment; filename=test_data_{datetime.now().strftime("%Y%m%d")}.csv'
        
        return response
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Log configurazione all'avvio
    logger.info("=== AVVIO SISTEMA TEST MANUTENZIONE PREDITTIVA ===")
    logger.info(f"Sampling Interval: {SAMPLING_INTERVAL}s")
    logger.info(f"Debug Mode: {DEBUG_MODE}")
    logger.info(f"URL Mapping configurato per {len(BLYNK_URLS)} pin")
    
    # Test connettività completo
    connectivity_results = blynk_client.test_connectivity()
    
    # Verifica se almeno pressure e pwm funzionano (minimi per algoritmo)
    critical_pins = ['pressure', 'pwm']
    critical_ok = all(connectivity_results.get(pin, {}).get('status') == 'OK' for pin in critical_pins)
    
    if critical_ok:
        logger.info("✓ Pin critici OK - Sistema pronto")
    else:
        logger.warning("⚠️  Alcuni pin critici non rispondono - Funzionamento limitato")
    
    # Avvia raccolta dati automaticamente se configurato
    if os.environ.get('AUTO_START', 'true').lower() == 'true':
        thread = threading.Thread(target=data_collection_loop, daemon=True)
        thread.start()
        logger.info("🚀 Raccolta dati avviata automaticamente")
    
    # Avvia server Flask
    port = int(os.environ.get('PORT', 80))
    logger.info(f"🌐 Avvio server Flask su porta {port}")
    app.run(host='0.0.0.0', port=port, debug=DEBUG_MODE)
