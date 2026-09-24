import os
from pymongo import MongoClient

# ВАЖНО: строка подключения — только через переменную окружения, никаких кредов в коде.
mongo_url = os.getenv("ANALYTICS_MONGO_URL", "mongodb://mongodb-analytics:27017/")
client = MongoClient(mongo_url)
db = client["analytics_db"]