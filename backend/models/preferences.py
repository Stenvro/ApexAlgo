from sqlalchemy import Column, Integer, String, JSON
from backend.core.database import Base

class Preference(Base):
    __tablename__ = "preferences"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)       # De naam van de instelling
    value = Column(JSON)                                # De inhoud (lekker flexibel door JSON)
