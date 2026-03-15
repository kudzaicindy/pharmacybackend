# Pharmacy Backend API

A Django REST Framework-based backend for pharmacy management system.

## Setup

1. Create a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

2. Install Python dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file (already created) with your configuration.

4. Run migrations (if using Django models):
```bash
python manage.py migrate
```

5. Create a superuser (optional):
```bash
python manage.py createsuperuser
```

6. Run the development server:
```bash
python manage.py runserver
```

The server will run on `http://localhost:8000` by default.

## API Endpoints

- `GET /` - Root endpoint
- `GET /health/` - Health check
- `GET /admin/` - Django admin panel
- `GET /api/` - API root

## Environment Variables

See `.env.example` for required environment variables.

## Project Structure

```
pharmacybackend/
├── pharmacybackend/     # Main project directory
│   ├── settings.py      # Django settings
│   ├── urls.py          # Main URL configuration
│   └── wsgi.py          # WSGI configuration
├── api/                 # API app
│   ├── models.py        # Database models
│   ├── views.py         # API views
│   ├── serializers.py   # DRF serializers
│   └── urls.py          # API URLs
└── manage.py            # Django management script

```
