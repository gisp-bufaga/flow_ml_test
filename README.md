# flow_ml_test
Pre-deploy testing to collect data and test flow, filter-wear and alert algorithm. Collecting data point for ML predictive algorithm


----

# README_BALENA.md - Guida Setup Balena
# 🌊 Deploy su Balena Cloud - Test Manutenzione Predittiva

## 🚀 Setup Rapido

### 1. Crea Applicazione Balena
```bash
# Installa Balena CLI
npm install balena-cli -g

# Login
balena login

# Crea app
balena app create predictive-maintenance-test --type raspberry-pi4-64

# Clona questo repository
git clone <your-repo>
cd predictive-maintenance-test

# Deploy
balena push predictive-maintenance-test
```

### 2. Configura Variabili Ambiente
Nel dashboard Balena, imposta le seguenti variabili:

| Variabile | Valore | Descrizione |
|-----------|--------|-------------|
| `BLYNK_TOKEN` | `3NAD9pyFu_uK1cZOFzpgfV1FqTHU_3RQ` | Token del tuo progetto Blynk |
| `BLYNK_SERVER` | `fra1.blynk.cloud` | Server Blynk (fra1, ny3, sgp1) |
| `SAMPLING_INTERVAL` | `30` | Intervallo campionamento (secondi) |
| `TEST_DURATION_HOURS` | `168` | Durata test (ore) - 168 = 1 settimana |
| `DEBUG_MODE` | `false` | Abilita logging dettagliato |
| `AUTO_START` | `true` | Avvio automatico raccolta dati |

### 3. Accesso Dashboard
- **URL Pubblico**: Abilitalo nel dashboard Balena
- **Tunnel SSH**: `balena tunnel <uuid> -p 80:80`
- **VPN**: Connetti dispositivo alla tua VPN Balena

## 📊 Funzionalità

### Dashboard Web Real-time
- **Metriche Live**: PWM, pressione, temperature, PM
- **Stato Filtri**: Usura, efficienza, ore di utilizzo
- **Confronto Portate**: Algoritmo vs Sensore Blynk
- **Grafici**: Andamento temporale e predizioni
- **Controlli**: Start/Stop test, Reset filtro
- **Export**: Download dati CSV

### API Endpoints
- `GET /api/current` - Dati attuali
- `GET /api/history/24` - Storia ultime 24h
- `GET /api/statistics` - Statistiche generali
- `POST /api/control` - Controllo test
- `GET /api/export` - Export CSV

### Dati Persistenti
- Database SQLite in volume `/data`
- Backup automatico
- Recovery dopo restart

## 🔧 Configurazione Blynk

### Pin Mapping
```python
BLYNK_PINS = {
    'pressure': 'v19',  # Pressione differenziale [Pa]
    'flow': 'v10',      # Portata sensore [m³/h]  
    'pwm': 'v1',        # PWM ventole [%]
    'temperature': 'v5', # Temperatura [°C]
    'pm_value': 'v6'    # Particolato [μg/m³]
}
```

### URL Blynk Utilizzati
Il sistema fa chiamate dirette agli endpoint:
- `https://fra1.blynk.cloud/external/api/get?token=<TOKEN>&v19` (Pressione)
- `https://fra1.blynk.cloud/external/api/get?token=<TOKEN>&v10` (Portata)
- `https://fra1.blynk.cloud/external/api/get?token=<TOKEN>&v1` (PWM)

## 🎯 Vantaggi Balena

### Gestione Remota
- **Deploy OTA**: Aggiornamenti senza accesso fisico
- **SSH/Terminal**: Accesso remoto completo
- **Logs Real-time**: Monitoring da dashboard
- **Health Check**: Status dispositivo automatico

### Scaling
- **Multi-device**: Stesso codice su più Raspberry
- **Fleet Management**: Gestione centralizzata
- **A/B Testing**: Deploy graduale aggiornamenti

### Monitoraggio
- **Metriche Sistema**: CPU, RAM, storage, temperatura
- **Network**: Connettività, traffico dati
- **Application**: Logs applicazione, restart automatici

## 📱 Accesso Mobile
Il dashboard è responsive e accessibile da:
- **Web Browser**: Qualsiasi dispositivo
- **Balena Cloud**: Dashboard integrata  
- **VPN**: Accesso sicuro da remoto

## 🔄 Workflow Tipico

1. **Deploy**: Push codice su Balena
2. **Config**: Imposta variabili ambiente
3. **Monitor**: Controlla dashboard per conferma avvio
4. **Test**: Lascia girare per durata desiderata
5. **Analysis**: Export dati e genera report
6. **Iterate**: Modifica algoritmo e re-deploy

## 🛡️ Sicurezza
- Tutti i dati restano nel dispositivo locale
- Accesso dashboard tramite autenticazione Balena
- Token Blynk configurabili come secrets
- Comunicazioni HTTPS
