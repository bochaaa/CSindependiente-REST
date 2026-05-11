# CSI Tenis Backend

Backend para administrar reservas de canchas de tenis del Club Sportivo Independiente.

## Tecnologías

- Python 3.14
- Django 6.0.5
- Django REST Framework 3.17.1

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

## Estructura inicial

- `csitenis/`: configuración del proyecto
- `reservations/`: app para administrar reservas de canchas
