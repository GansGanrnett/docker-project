from fastapi import FastAPI
from app.core.database import db
app = FastAPI()
@app.get("/health")
def health():
    return {"status": "Analytics Service OK"}
@app.get("/events")
def list_events():
    return list(db.events.find({}, {"_id": 0}))
