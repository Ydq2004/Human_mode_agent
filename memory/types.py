from enum import Enum


class MemoryType(str, Enum):
    ENTITY = "entity"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    INTERACTION_PATTERN = "interaction_pattern"
    EVENT = "event"
    KNOWLEDGE = "knowledge"
    OTHER = "other"


class RelationType(str, Enum):
    RELATED_TO = "related_to"
    BELONGS_TO = "belongs_to"
    REFERS_TO = "refers_to"
    SIMILAR_TO = "similar_to"


ALLOWED_MEMORY_TYPES = {item.value for item in MemoryType}
ALLOWED_RELATION_TYPES = {item.value for item in RelationType}