# Land Scout deployment-ready package

Prepared for hosting under:
- `landscout.rrvconstruction.com`
- or `app.rrvconstruction.com`

## What it is
A mobile-friendly installable PWA for checking Florida land listings against FEMA flood data.

## Counties prepared
- Lake
- Orange
- Polk
- Marion
- Volusia
- Sumter

## Local run
```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Open:
```bash
http://127.0.0.1:8000
```

## Optional environment variables
```bash
APP_NAME="Land Scout"
APP_DOMAIN="landscout.rrvconstruction.com"
APP_BRAND="RRV Construction"
```

## Render deployment
Build command:
```bash
pip install -r requirements.txt
```

Start command:
```bash
uvicorn app:app --host 0.0.0.0 --port 10000
```

## Custom domain flow
1. Deploy to Render
2. Add custom domain: `landscout.rrvconstruction.com`
3. Create the DNS record Render gives you
4. Wait for SSL
5. Open the domain on your phone
6. Add to home screen

## iPhone install
Safari -> Share -> Add to Home Screen

## Android install
Chrome -> Install App or Add to Home Screen

## Honest limitation
Parcel-only listings still need deeper county-specific automation for true one-tap parcel-to-coordinate resolution.
