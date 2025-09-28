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



# Configurazione da variabili ambiente Balena
BLYNK_TOKEN = os.environ.get('BLYNK_TOKEN', 'bNr-NaQgxRioKbUXWiYDsQ1J6P2MR-gK')  #Dispositivo Milano UV 
BLYNK_SERVER = os.environ.get('BLYNK_SERVER', 'fra1.blynk.cloud')
SAMPLING_INTERVAL = int(os.environ.get('SAMPLING_INTERVAL', '30'))  # secondi
TEST_DURATION_HOURS = int(os.environ.get('TEST_DURATION_HOURS', '168'))  # 1 settimana
DEBUG_MODE = os.environ.get('DEBUG_MODE', 'true').lower() == 'true'

# Mappa pin Blynk
BLYNK_PINS = {
    'pressure': 'v19',
    'flow': 'v10', 
    'pwm': 'v26',
    'temperature': 'v8',
    'pm_value': 'v4'
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

class BlynkSimpleClient:
    """Client Blynk semplificato per singoli stream"""
    
    def __init__(self, token: str, server: str):
        self.token = token
        self.base_url = f"https://{server}/external/api"
        self.session = requests.Session()
        self.session.timeout = 10
        
    def get_pin_value(self, pin: str) -> float:
        """Ottieni valore singolo pin"""
        try:
            url = f"{self.base_url}/get?token={self.token}&{pin}"
            response = self.session.get(url)
            response.raise_for_status()
            
            # Blynk restituisce lista o valore diretto
            data = response.json()
            if isinstance(data, list):
                return float(data[0]) if data else 0.0
            return float(data)
            
        except Exception as e:
            logger.error(f"Errore lettura pin {pin}: {e}")
            return 0.0
    
    def get_multiple_pins(self, pins: dict) -> dict:
        """Ottieni valori multipli pin in parallelo"""
        results = {}
        
        for name, pin in pins.items():
            value = self.get_pin_value(pin)
            results[name] = value
            
        return results

class PredictiveAlgorithm:
    """Algoritmo manutenzione predittiva ottimizzato"""
    
    def __init__(self):
        # Curve ventole semplificate (punti chiave)
        self.fan_flow = np.array([0, 500, 1000, 1500, 2000, 2500, 2950])
        self.fan_pressure = np.array([450, 320, 240, 180, 120, 70, 20])
        
        # Curve filtri
        self.filter_flow = np.array([275, 1305, 2179, 3029, 4118, 5160, 5711, 6177, 6872])  # *4 filtri
        self.filter_pressure = np.array([2.25, 13.25, 22.75, 34.25, 51.25, 68.5, 79.0, 88.25, 101.75])
        
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
        Converte la percentuale PWM da Blynk alla velocità reale delle ventole
        
        Args:
            blynk_pwm_percent: Percentuale PWM pubblicata su Blynk (1-100%)
            
        Returns:
            Percentuale velocità reale delle ventole per scalare le curve
        """
        if blynk_pwm_percent == 0:
            return 0.0
            
        # Converte da percentuale Blynk (1-100%) a duty cycle PWM (64-153)
        duty_cycle = self.map_value(blynk_pwm_percent, 1, 100, self.MIN_FAN_SPEED_PWM, self.MAX_FAN_SPEED_PWM)
        
        # Converte duty cycle a percentuale velocità reale (25-60% del PWM fisico)
        real_pwm_percent = self.map_value(duty_cycle, self.MIN_FAN_SPEED_PWM, self.MAX_FAN_SPEED_PWM, 25, 60)
        
        # Per le curve delle ventole, normalizza su 100% (dove 60% PWM = 100% velocità)
        fan_speed_percent = self.map_value(real_pwm_percent, 25, 60, 25, 100)
        
        return fan_speed_percent
        
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
        
        # Scala curva ventole per PWM
        scale_factor = system_data.pwm_percentage / 100.0
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
        filter_change_needed = (
            obstruction_index >= self.MAX_OBSTRUCTION_CRITICAL or
            self.hours_since_change >= self.FILTER_CHANGE_HOURS
        )
        
        system_anomaly = (
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

# Istanze globali
blynk_client = BlynkSimpleClient(BLYNK_TOKEN, BLYNK_SERVER)
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
            # Ottieni dati da Blynk
            blynk_data = blynk_client.get_multiple_pins(BLYNK_PINS)
            
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
                logger.info(f"PWM: {system_data.pwm_percentage:.0f}% | "
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

@app.route('/api/current')
def api_current():
    """API dati attuali"""
    try:
        # Ottieni dati Blynk
        blynk_data = blynk_client.get_multiple_pins(BLYNK_PINS)
        
        system_data = SystemData(
            timestamp=datetime.now().isoformat(),
            pwm_percentage=blynk_data.get('pwm', 0),
            pressure_measured=blynk_data.get('pressure', 0),
            flow_blynk=blynk_data.get('flow', 0),
            temperature=blynk_data.get('temperature', 20),
            pm_value=blynk_data.get('pm_value', 0)
        )
        
        metrics = algorithm.calculate_metrics(system_data)
        
        return jsonify({
            'system_data': asdict(system_data),
            'metrics': asdict(metrics),
            'test_stats': test_stats,
            'status': 'running' if test_running else 'stopped'
        })
        
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
    # Avvia raccolta dati automaticamente
    if os.environ.get('AUTO_START', 'true').lower() == 'true':
        thread = threading.Thread(target=data_collection_loop, daemon=True)
        thread.start()
    
    # Avvia server Flask
    port = int(os.environ.get('PORT', 80))
    app.run(host='0.0.0.0', port=port, debug=DEBUG_MODE)
