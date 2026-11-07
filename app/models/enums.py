from enum import Enum


class AccountType(str, Enum):
    SCHOOL_ADMIN = "SCHOOL_ADMIN"
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    PLATFORM_OWNER = "PLATFORM_OWNER"
    