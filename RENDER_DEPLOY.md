# Deploying on Render

This is a **Django** app. Use **gunicorn**, not uvicorn.

## Start Command (required)

In Render → your service → **Settings** → **Build & Deploy** → **Start Command**, set:

```bash
gunicorn pharmacybackend.wsgi:application --bind 0.0.0.0:$PORT
```

If you see `uvicorn: command not found`, the start command was set for FastAPI; change it to the line above.

---

## Build failures: setuptools / numpy

If the default build fails with **"Cannot import 'setuptools.build_meta'"**, do one of the following.

## Option 1: Use the build script (recommended)

In the Render dashboard for your service:

1. Go to **Settings** → **Build & Deploy**.
2. Set **Build Command** to:
   ```bash
   chmod +x build.sh && ./build.sh
   ```
   Or, if the repo root is the service root:
   ```bash
   chmod +x build.sh && ./build.sh
   ```

This installs `pip`, `setuptools`, and `wheel` before installing `requirements.txt`, so any package that must build from source can succeed.

## Option 2: Custom build command without script

Set **Build Command** to:

```bash
pip install --upgrade pip setuptools wheel && pip install -r requirements.txt
```

## Option 3: If a package still builds from source

If you still see the setuptools error, set **Build Command** to:

```bash
pip install --upgrade pip setuptools wheel && PIP_NO_BUILD_ISOLATION=1 pip install -r requirements.txt
```

This uses the environment’s setuptools when building sdists instead of an isolated build env.

---

**Python version:** Ensure `.python-version` contains `3.12` so Render does not use Python 3.14.
