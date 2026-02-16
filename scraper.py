import httpx
from bs4 import BeautifulSoup
from datetime import datetime
import re

async def scrape_parco_news():
    """Scrapa le ultime news dal sito del Parco Castelli Romani"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://www.parcocastelliromani.it")
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Cerca articoli/news (da adattare in base alla struttura del sito)
            news = []
            
            # Prova a trovare elementi comuni per le news
            articles = soup.find_all(['article', 'div'], class_=re.compile(r'(news|post|article|item)', re.I), limit=5)
            
            for article in articles:
                title_elem = article.find(['h1', 'h2', 'h3', 'h4', 'a'])
                date_elem = article.find(['time', 'span'], class_=re.compile(r'date', re.I))
                link_elem = article.find('a', href=True)
                
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    date = date_elem.get_text(strip=True) if date_elem else "Data non disponibile"
                    link = link_elem['href'] if link_elem else "#"
                    
                    # Assicurati che il link sia assoluto
                    if link.startswith('/'):
                        link = f"https://www.parcocastelliromani.it{link}"
                    
                    # Filtra per parole chiave rilevanti
                    keywords = ['chiusura', 'sentiero', 'taglio', 'boschivo', 'ordinanza', 'divieto', 'allerta', 'incendio']
                    if any(keyword in title.lower() for keyword in keywords):
                        news.append({
                            'title': title,
                            'date': date,
                            'link': link,
                            'source': 'Parco Castelli Romani',
                            'priority': 'high'
                        })
            
            return news[:5]  # Massimo 5 news
            
    except Exception as e:
        print(f"Errore scraping Parco: {e}")
        return [{
            'title': 'Impossibile recuperare news dal sito del Parco',
            'date': datetime.now().strftime('%d/%m/%Y'),
            'link': 'https://www.parcocastelliromani.it',
            'source': 'Parco Castelli Romani',
            'priority': 'info'
        }]

async def get_protezione_civile_alerts():
    """Recupera allerte meteo dalla Protezione Civile Lazio"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # URL del bollettino Protezione Civile Lazio
            response = await client.get("http://www.regione.lazio.it/protezione_civile")
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            alerts = []
            # Cerca allerte meteo
            alert_elements = soup.find_all(['div', 'article'], class_=re.compile(r'(alert|allerta|avviso)', re.I), limit=3)
            
            for alert in alert_elements:
                title_elem = alert.find(['h1', 'h2', 'h3', 'strong'])
                if title_elem:
                    alerts.append({
                        'title': title_elem.get_text(strip=True),
                        'date': datetime.now().strftime('%d/%m/%Y'),
                        'link': 'http://www.regione.lazio.it/protezione_civile',
                        'source': 'Protezione Civile Lazio',
                        'priority': 'high'
                    })
            
            return alerts
            
    except Exception as e:
        print(f"Errore scraping Protezione Civile: {e}")
        return []

async def get_incendio_alerts():
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
                'description': 'Divieto di accesso ai boschi nelle ore più calde (11:00-18:00). Verificare ordinanze comunali.'
            }]
    
    return []

def get_static_alerts():
    """Restituisce avvisi statici sempre validi"""
    return [
        {
            'title': 'Verifica chiusure sentieri su Trailforks',
            'date': 'Sempre aggiornato',
            'link': 'https://www.trailforks.com/region/castelli-romani/',
            'source': 'Community MTB',
            'priority': 'info',
            'description': 'Controlla le segnalazioni in tempo reale della community biker'
        },
        {
            'title': 'Albi Pretori Comunali - Ordinanze e Tagli Boschivi',
            'date': 'Aggiornamento continuo',
            'link': 'https://www.comune.roccadipapa.rm.it/albo-pretorio',
            'source': 'Comuni Castelli Romani',
            'priority': 'info',
            'description': 'Controlla gli albi pretori dei comuni per ordinanze di chiusura e autorizzazioni taglio boschi'
        }
    ]

async def get_all_alerts():
    """Raccoglie tutti gli avvisi da diverse fonti"""
    all_alerts = []
    
    # News dal Parco
    parco_news = await scrape_parco_news()
    all_alerts.extend(parco_news)
    
    # Allerte Protezione Civile
    pc_alerts = await get_protezione_civile_alerts()
    all_alerts.extend(pc_alerts)
    
    # Allerte incendi
    incendio_alerts = await get_incendio_alerts()
    all_alerts.extend(incendio_alerts)
    
    # Avvisi statici
    static_alerts = get_static_alerts()
    all_alerts.extend(static_alerts)
    
    # Ordina per priorità: critical > high > info
    priority_order = {'critical': 0, 'high': 1, 'info': 2}
    all_alerts.sort(key=lambda x: priority_order.get(x.get('priority', 'info'), 2))
    
    return all_alerts
