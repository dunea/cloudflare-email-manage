"""ORM 模型包：集中导入所有模型，确保注册到 Base.metadata。"""

from app.models.api_key import APIKey
from app.models.cf_account import CFAccount
from app.models.destination_address import DestinationAddress
from app.models.domain import Domain
from app.models.domain_assignment import DomainAssignment
from app.models.email_address import EmailAddress
from app.models.forwarding_rule import ForwardingRule
from app.models.inbound_email import InboundEmail
from app.models.user import User

__all__ = [
    "APIKey",
    "CFAccount",
    "DestinationAddress",
    "Domain",
    "DomainAssignment",
    "EmailAddress",
    "ForwardingRule",
    "InboundEmail",
    "User",
]
