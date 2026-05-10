import os
import io
import uuid
import fitz  # PyMuPDF
from PIL import Image
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from typing import Optional
import requests
from datetime import datetime

app = FastAPI(title="Floraputation V5 Worker")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://floraputation-v5.vercel.app",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


def process_pdf(upload_id: str):
    try:
        # 1. Update status to processing
        supabase.table("uploads").update({"status": "processing"}).eq("id", upload_id).execute()
        
        # 2. Get upload details
        res = supabase.table("uploads").select("*").eq("id", upload_id).single().execute()
        upload_data = res.data
        user_id = upload_data.get("user_id")
        
        # 3. Download PDF from private bucket using service_role
        # The storage path is: {user_id}/{upload_id}.pdf
        storage_path = f"{user_id}/{upload_id}.pdf"
        pdf_response = supabase.storage.from_("catalogs").download(storage_path)
        pdf_content = pdf_response
        
        doc = fitz.open(stream=pdf_content, filetype="pdf")
        
        # Update page count
        supabase.table("uploads").update({"page_count": len(doc)}).eq("id", upload_id).execute()
        
        # 4. Process each page
        errors = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # L1: Try to extract images directly
            images = page.get_images(full=True)
            
            if images:
                for img_index, img in enumerate(images):
                    try:
                        xref = img[0]
                        base_image = doc.extract_image(xref)
                        image_bytes = base_image["image"]
                        
                        # Process image
                        handle_extracted_image(upload_id, user_id, page_num, image_bytes, f"p{page_num}_i{img_index}")
                    except Exception as img_err:
                        errors.append(f"Page {page_num}, img {img_index}: {str(img_err)}")
                        continue
            else:
                # L2: Fallback to high-res rendering
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x scale for better quality
                    image_bytes = pix.tobytes("png")
                    handle_extracted_image(upload_id, user_id, page_num, image_bytes, f"p{page_num}_render")
                except Exception as render_err:
                    errors.append(f"Page {page_num} render: {str(render_err)}")
                    continue
        
        # 5. Mark as completed (even if some individual images had errors)
        if errors:
            print(f"Completed with {len(errors)} errors: {errors[:5]}")
        
        supabase.table("uploads").update({
            "status": "completed",
            "completed_at": datetime.utcnow().isoformat()
        }).eq("id", upload_id).execute()
        
    except Exception as e:
        print(f"Error processing {upload_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        supabase.table("uploads").update({"status": "failed"}).eq("id", upload_id).execute()


def handle_extracted_image(upload_id: str, user_id: str, page_num: int, image_bytes: bytes, suffix: str):
    # 1. Smart Crop (1:1 center crop)
    img = Image.open(io.BytesIO(image_bytes))
    
    # Convert to RGB if necessary (e.g., CMYK or RGBA)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    
    width, height = img.size
    
    # Skip very small images (likely icons/logos)
    if width < 50 or height < 50:
        return
    
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
    
    # 2. Upload to Supabase Storage (varieties bucket - public)
    file_name = f"{upload_id}_{suffix}.jpg"
    storage_path = f"extracted/{upload_id}/{file_name}"
    
    try:
        supabase.storage.from_("varieties").upload(
            path=storage_path,
            file=processed_bytes,
            file_options={"content-type": "image/jpeg"}
        )
    except Exception as upload_err:
        # File might already exist, try to overwrite
        if "Duplicate" in str(upload_err) or "already exists" in str(upload_err):
            supabase.storage.from_("varieties").update(
                path=storage_path,
                file=processed_bytes,
                file_options={"content-type": "image/jpeg"}
            )
        else:
            raise upload_err
    
    public_url = supabase.storage.from_("varieties").get_public_url(storage_path)
    
    # 3. Quality score based on image dimensions
    quality_score = min(100, int((width * height) / (500 * 500) * 85))
    quality_score = max(30, min(100, quality_score))
    
    # 4. Save to extracted_images table
    supabase.table("extracted_images").insert({
        "upload_id": upload_id,
        "page_number": page_num + 1,
        "quality_score": quality_score,
        "processed_image_url": public_url,
        "raw_image_url": public_url,
        "detected_crop": "Unknown",
        "detected_variety": f"Variety {suffix}",
        "confidence_score": 50,
    }).execute()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
