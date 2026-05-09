import os
import io
import uuid
import fitz  # PyMuPDF
from PIL import Image
from fastapi import FastAPI, BackgroundTasks, HTTPException
from supabase import create_client, Client
from typing import Optional
import requests
from datetime import datetime

app = FastAPI(title="Floraputation V5 Worker")

# Supabase Config
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://issvdwkurrdpeynrfobh.supabase.co")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_SERVICE_KEY else None

@app.get("/")
async def root():
    return {"status": "online", "service": "Floraputation V5 Worker"}

@app.get("/status/{upload_id}")
async def get_status(upload_id: str):
    if not supabase:
        return {"error": "Supabase not configured"}
    
    res = supabase.table("uploads").select("*").eq("id", upload_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Upload not found")
    
    return res.data

@app.post("/process/{upload_id}")
async def trigger_processing(upload_id: str, background_tasks: BackgroundTasks):
    if not supabase:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    
    # Check if upload exists
    res = supabase.table("uploads").select("*").eq("id", upload_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Upload not found")
    
    # Start background task
    background_tasks.add_task(process_pdf, upload_id)
    
    return {"message": "Processing started", "upload_id": upload_id}

async def process_pdf(upload_id: str):
    try:
        # 1. Update status to processing
        supabase.table("uploads").update({"status": "processing"}).eq("id", upload_id).execute()
        
        # 2. Get upload details
        res = supabase.table("uploads").select("*").eq("id", upload_id).single().execute()
        upload_data = res.data
        file_url = upload_data.get("file_url")
        user_id = upload_data.get("user_id")
        
        if not file_url:
            raise Exception("No file URL found")
            
        # 3. Download PDF
        response = requests.get(file_url)
        pdf_content = response.content
        doc = fitz.open(stream=pdf_content, filetype="pdf")
        
        # Update page count
        supabase.table("uploads").update({"page_count": len(doc)}).eq("id", upload_id).execute()
        
        # 4. Process each page
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # L1: Try to extract images directly
            images = page.get_images(full=True)
            
            if images:
                for img_index, img in enumerate(images):
                    xref = img[0]
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    
                    # Process image
                    await handle_extracted_image(upload_id, user_id, page_num, image_bytes, f"p{page_num}_i{img_index}")
            else:
                # L2: Fallback to high-res rendering
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x scale for better quality
                image_bytes = pix.tobytes("png")
                await handle_extracted_image(upload_id, user_id, page_num, image_bytes, f"p{page_num}_render")
        
        # 5. Mark as completed
        supabase.table("uploads").update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat()
        }).eq("id", upload_id).execute()
        
    except Exception as e:
        print(f"Error processing {upload_id}: {str(e)}")
        supabase.table("uploads").update({"status": "failed"}).eq("id", upload_id).execute()

async def handle_extracted_image(upload_id: str, user_id: str, page_num: int, image_bytes: bytes, suffix: str):
    # 1. Smart Crop (1:1 center crop)
    img = Image.open(io.BytesIO(image_bytes))
    width, height = img.size
    new_size = min(width, height)
    left = (width - new_size) / 2
    top = (height - new_size) / 2
    right = (width + new_size) / 2
    bottom = (height + new_size) / 2
    img_cropped = img.crop((left, top, right, bottom))
    
    # Save to buffer
    buffer = io.BytesIO()
    img_cropped.save(buffer, format="JPEG", quality=90)
    processed_bytes = buffer.getvalue()
    
    # 2. Upload to Supabase Storage (varieties bucket)
    file_name = f"{upload_id}_{suffix}.jpg"
    storage_path = f"extracted/{upload_id}/{file_name}"
    
    supabase.storage.from_("varieties").upload(
        path=storage_path,
        file=processed_bytes,
        file_options={"content-type": "image/jpeg"}
    )
    
    public_url = supabase.storage.from_("varieties").get_public_url(storage_path)
    
    # 3. AI Naming Detection (Mock for now, in real app would use Vision LLM)
    # For this demo, we'll just create a variety record if it looks like a good image
    quality_score = 85 # Mock score
    
    # 4. Save to extracted_images table
    extracted_res = supabase.table("extracted_images").insert({
        "upload_id": upload_id,
        "page_number": page_num + 1,
        "quality_score": quality_score,
        "processed_image_url": public_url,
        "raw_image_url": public_url, # In real app, save raw too
        "detected_crop": "Petunia", # Mock
        "detected_variety": f"Variety {suffix}", # Mock
    }).execute()
    
    # 5. Create a variety record (Auto-promote for now)
    if quality_score > 80:
        supabase.table("varieties").insert({
            "crop": "Petunia",
            "variety": f"Variety {suffix}",
            "image_url": public_url,
            "quality_score": quality_score,
            "source_upload_id": upload_id,
            "created_by": user_id
        }).execute()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
