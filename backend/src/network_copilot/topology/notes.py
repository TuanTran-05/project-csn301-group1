"""Notes and groups drawn on the canvas."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import NotFoundError
from ..extensions import db
from ..projects.service import validate
from .model import TopologyNote

MAX_NOTES = 200
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class NoteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["note", "group"] = "note"
    text: str = Field(min_length=1, max_length=500)
    x: float = Field(default=0, ge=-100000, le=100000)
    y: float = Field(default=0, ge=-100000, le=100000)
    width: float = Field(default=160, gt=10, le=5000)
    height: float = Field(default=90, gt=10, le=5000)
    color: str | None = None

    @field_validator("color")
    @classmethod
    def check_color(cls, value):
        if value is not None and not COLOR.match(value):
            raise ValueError("color must look like #aabbcc")
        return value


class NoteUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str | None = Field(default=None, min_length=1, max_length=500)
    x: float | None = Field(default=None, ge=-100000, le=100000)
    y: float | None = Field(default=None, ge=-100000, le=100000)
    width: float | None = Field(default=None, gt=10, le=5000)
    height: float | None = Field(default=None, gt=10, le=5000)
    color: str | None = None

    @field_validator("color")
    @classmethod
    def check_color(cls, value):
        return NoteSchema.check_color(value)


def list_notes(project_id: int) -> list[TopologyNote]:
    return db.session.query(TopologyNote).filter_by(project_id=project_id).order_by(TopologyNote.id).all()


def get_note(project_id: int, note_id: int) -> TopologyNote:
    note = db.session.query(TopologyNote).filter_by(project_id=project_id, id=note_id).first()
    if note is None:
        raise NotFoundError("Note not found.")
    return note


def create_note(project, payload: dict) -> TopologyNote:
    from ..errors import ConflictError

    data = validate(NoteSchema, payload, "Note")
    if db.session.query(TopologyNote).filter_by(project_id=project.id).count() >= MAX_NOTES:
        raise ConflictError(f"A project holds at most {MAX_NOTES} notes and groups.")
    note = TopologyNote(project_id=project.id, **data.model_dump())
    db.session.add(note)
    db.session.commit()
    return note


def update_note(project, note_id: int, payload: dict) -> TopologyNote:
    note = get_note(project.id, note_id)
    data = validate(NoteUpdateSchema, payload, "Note")
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is None and field != "color":
            continue
        setattr(note, field, value)
    db.session.commit()
    return note


def delete_note(project, note_id: int) -> None:
    db.session.delete(get_note(project.id, note_id))
    db.session.commit()
