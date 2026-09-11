from fastapi import FastAPI
from app.core.database import engine, Base
app = FastAPI()
@app.on_event("startup")
def create_tables():
    Base.metadata.create_all(bind=engine)
@app.get("/health")
def health():
    return {"status": "Payment Service OK"}
