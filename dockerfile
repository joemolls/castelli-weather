FROM python:3.11-slim

WORKDIR /app

# Copia i requirements
COPY requirements.txt .

# Installa le dipendenze
RUN pip install --no-cache-dir -r requirements.txt

# Copia tutto il codice
COPY . .

# Esponi la porta 8080
EXPOSE 8080

# Comando per avviare l'app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
