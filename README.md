# 🏔️ Castelli Romani Weather Dashboard

Dashboard meteo per i Castelli Romani con previsioni a 72 ore.

## 🚀 Quick Start con Docker

### Costruire l'immagine
```bash
docker build -t castelli-weather .
```

### Eseguire il container
```bash
docker run -p 8080:8080 castelli-weather
```

Apri il browser su: `http://localhost:8080`

## 🐳 Con Docker Compose
```bash
docker-compose up
```

## 📦 Deploy su varie piattaforme

### Render.com
1. Connetti il repository GitHub
2. Seleziona "Docker" come ambiente
3. Deploy automatico!

### Railway.app
1. Connetti il repository
2. Railway rileva automaticamente il Dockerfile
3. Deploy!

### Fly.io
```bash
fly launch
fly deploy
```

## 📊 Località coperte

- Monte Cavo (949m)
- Maschio delle Faete (956m)
- Maschio d'Artemisio (812m)
- Fontana Tempesta (750m)
- Rocca Priora (768m)

## 🔧 Tecnologie

- FastAPI
- Chart.js
- Open-Meteo API

## 📄 Licenza

Dati meteo da [Open-Meteo](https://open-meteo.com) (CC BY 4.0)
```

## 6. **.gitignore**
```
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
env/
ENV/
.venv

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Project specific
*.log
