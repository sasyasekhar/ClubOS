
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from uuid import UUID


class SchoolRegistration(BaseModel):
    school_name: str = Field(min_length=2, max_length=200)
    admin_full_name: str = Field(min_length=2, max_length=150)
    username: str = Field(min_length=3, max_length=80)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class SchoolRegistrationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    school_id: UUID
    school_code: str
    admin_account_id: UUID

