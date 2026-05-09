# Floraputation V5 Worker

Python FastAPI service for processing flower variety PDF catalogs.

## Features
- PDF extraction using PyMuPDF (L1)
- High-res rendering fallback (L2)
- Smart 1:1 center crop
- Supabase integration (Storage & Database)

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set environment variables:
   ```bash
   export SUPABASE_URL=your_project_url
   export SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
   ```
3. Run the service:
   ```bash
   uvicorn main:app --reload
   ```

## Deployment
This service is ready to be deployed to [Render](https://render.com) using the included `render.yaml` or via Docker using the `Dockerfile`.
