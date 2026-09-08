from pymongo import MongoClient

client = MongoClient("mongodb://mongodb-analytics:27017/")
db = client["analytics_db"]
