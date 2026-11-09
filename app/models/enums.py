from enum import Enum


class AccountType(str, Enum):
    SCHOOL_ADMIN = "SCHOOL_ADMIN"
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"
    PLATFORM_OWNER = "PLATFORM_OWNER"


class AcademicYearStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


class ClubStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class ClubReviewDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ClubRole(str, Enum):
    STUDENT_REP = "STUDENT_REP"
    TEACHER_REP = "TEACHER_REP"


class MembershipStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    DECLINED = "DECLINED"
    ENDED = "ENDED"

class ActivityRevisionStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ActivityReviewDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ReviewMode(str, Enum):
    DIRECT = "DIRECT"
    IMPERSONATION = "IMPERSONATION"

class LoginOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ImpersonationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"
    EXPIRED = "EXPIRED"


class DeletionStatus(str, Enum):
    PENDING = "PENDING"
    RESTORED = "RESTORED"
    PERMANENTLY_DELETED = "PERMANENTLY_DELETED"