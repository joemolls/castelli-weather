from datetime import datetime

def get_incendio_alerts():
    """Verifica periodo di divieto accesso boschi (periodo estivo)"""
    now = datetime.now()
    
    # Periodo critico incendi: 15 giugno - 30 settembre
    if 6 <= now.month <= 9:
        if (now.month == 6 and now.day >= 15) or (now.month in [7, 8]) or (now.month == 9 and now.day <= 30):
            return [{
                'title': '🔥 PERIODO CRITICO INCENDI - Divieti in vigore',
                'date': now.strftime('%d/%m/%Y'),
                'link': 'http://www.regione.lazio.it/protezione_civile',
                'source': 'Sistema AIB Lazio',
                'priority': 'critical',
                'description': 'Divieto di accesso ai boschi nelle ore più calde (11:00-18:00). Verificare ordinanze comunali specifiche prima di uscire.'
            }]
    
    return []

def get_static_alerts():
    """Restituisce avvisi statici sempre validi e fonti ufficiali da controllare"""
    now = datetime.now()
    
    return [
        {
            'title': '📋 Verifica sempre le fonti ufficiali prima di uscire',
            'date': now.strftime('%d/%m/%Y'),
            'link': 'https://www.parcocastelliromani.it',
            'source': 'Importante',
            'priority': 'high',
            'description': 'Le ordinanze di chiusura sentieri e i tagli boschivi sono pubblicati sul sito del Parco e negli Albi Pretori comunali. Controlla sempre prima di ogni uscita.'
        },
        {
            'title': '🚵 Stato Sentieri in Tempo Reale - Community MTB',
            'date': 'Aggiornamento continuo',
            'link': 'https://www.trailforks.com/region/castelli-romani/',
            'source': 'Trailforks',
            'priority': 'info',
            'description': 'I biker segnalano in tempo reale chiusure, fango, alberi caduti e condizioni dei sentieri. Controlla prima di uscire e contribuisci anche tu!'
        },
        {
            'title': '📜 Albi Pretori Comunali - Ordinanze e Tagli Boschivi',
            'date': 'Aggiornamento quotidiano',
            'link': 'https://www.comune.roccadipapa.rm.it/albo-pretorio',
            'source': 'Comuni Castelli Romani',
            'priority': 'info',
            'description': 'Le autorizzazioni per tagli boschivi e le ordinanze di chiusura sono pubblicate negli albi pretori. Cerca "taglio", "utilizzazione boschiva" o "chiusura".'
        },
        {
            'title': '🚨 Protezione Civile Lazio - Allerte Meteo',
            'date': 'Aggiornamento giornaliero',
            'link': 'http://www.regione.lazio.it/protezione_civile',
            'source': 'Regione Lazio',
            'priority': 'info',
            'description': 'Consulta le allerte meteo ufficiali, i bollettini di criticità e i divieti di accesso ai boschi per rischio incendi.'
        }
    ]

async def get_all_alerts():
    """Raccoglie tutti gli avvisi da diverse fonti"""
    all_alerts = []
    
    # Allerte incendi (periodo estivo)
    incendio_alerts = get_incendio_alerts()
    all_alerts.extend(incendio_alerts)
    
    # Avvisi statici e link alle fonti ufficiali
    static_alerts = get_static_alerts()
    all_alerts.extend(static_alerts)
    
    # Ordina per priorità: critical > high > info
    priority_order = {'critical': 0, 'high': 1, 'info': 2}
    all_alerts.sort(key=lambda x: priority_order.get(x.get('priority', 'info'), 2))
    
    return all_alerts
