"""The Thumbnail tab: the picture comes from Flow, the words from here.

The sheet describes the picture, in a column rather than a row so it never
becomes a clip in the video. Everything laid over it is set here, as a list
of text blocks: a thumbnail reads as one shout plus a smaller remark, and
those want different fonts, sizes and places, so they are separate things
rather than one styled string.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from imgen.config import TextBlock
from imgen.platforms import display_font, installed_fonts, open_path

ALIGNS = ["left", "centre", "right"]
POSITIONS = ["top", "middle", "bottom"]

#: Ready-made looks, so a new block is usable before anything is touched.
#: The faces are resolved against this machine: naming one it does not
#: have means the page silently picks another and the thumbnail is wrong
#: with no warning. A stock Pop!_OS has no Arial Black.
_HEAVY = display_font()
_NARROW = display_font((
    "Fira Sans Condensed", "Liberation Sans Narrow", "DejaVu Sans Condensed",
    "Fira Sans", "DejaVu Sans",
))

PRESETS = {
    "Shout": dict(
        font=_HEAVY, size=108, colour="#FFFFFF", outline="#101010",
        outline_px=13, align="centre", position="middle",
    ),
    "Remark": dict(
        font=_NARROW, size=40, weight=700, colour="#FFD400",
        outline="#101010", outline_px=6, uppercase=False,
        align="centre", position="bottom", dy=-8,
    ),
    "Badge": dict(
        font=_HEAVY, size=44, colour="#111111",
        outline_px=0, shadow=False, box="#FFD400", align="left",
        position="top", dx=6, dy=6, rotate=-3.0, tracking=0.04,
    ),
    "Corner note": dict(
        font=_NARROW, size=34, weight=700, colour="#FFFFFF",
        outline="#101010", outline_px=5, uppercase=False,
        align="right", position="bottom",
    ),
}


class ThumbnailPanel(QWidget):
    blocks_changed = Signal(list)   # list of block dicts
    queue_requested = Signal()
    build_requested = Signal()
    suggest_requested = Signal()

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.project = None
        self.blocks: list[TextBlock] = []
        self._loading = False

        # ---- the picture ------------------------------------------------
        self.description = QLabel("—")
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color: #9aa0a6;")
        self.btn_queue = QPushButton("Queue the picture")
        self.btn_queue.setToolTip(
            "Adds the sheet's thumbnail description to the queue, so the next "
            "run generates it in Flow like any other image."
        )
        self.btn_queue.clicked.connect(self.queue_requested)

        # ---- the list of blocks -----------------------------------------
        self.list = QListWidget()
        self.list.setFixedWidth(190)
        self.list.currentRowChanged.connect(self._select)

        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        self.btn_add = QPushButton("Add")
        self.btn_add.clicked.connect(self._add)
        self.btn_copy = QPushButton("Duplicate")
        self.btn_copy.clicked.connect(self._duplicate)
        self.btn_del = QPushButton("Remove")
        self.btn_del.clicked.connect(self._remove)
        self.btn_up = QPushButton("↑")
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down = QPushButton("↓")
        self.btn_down.clicked.connect(lambda: self._move(1))
        self.btn_suggest = QPushButton("Suggest from the sheet")
        self.btn_suggest.setToolTip(
            "Picks the strongest lines the script already contains. It "
            "chooses wording, it does not invent any."
        )
        self.btn_suggest.clicked.connect(self.suggest_requested)

        # ---- the editor for one block ------------------------------------
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("The words. One line here is one line there.")
        self.text.setFixedHeight(66)
        self.text.textChanged.connect(lambda: self._set(text=self.text.toPlainText()))

        self.font = QComboBox()
        self.font.setEditable(True)
        self.font.addItems(installed_fonts())
        self.font.setMinimumWidth(190)
        self.font.currentTextChanged.connect(lambda v: self._set(font=v))

        self.size = self._spin(20, 300, " px", lambda v: self._set(size=v))
        self.weight = self._spin(100, 900, "", lambda v: self._set(weight=v), step=100)
        self.outline_px = self._spin(0, 40, " px", lambda v: self._set(outline_px=v))
        self.dx = self._spin(-600, 600, " px", lambda v: self._set(dx=v))
        self.dy = self._spin(-400, 400, " px", lambda v: self._set(dy=v))
        self.box_pad = self._spin(0, 80, " px", lambda v: self._set(box_pad=v))

        self.rotate = self._dspin(-45, 45, 1.0, lambda v: self._set(rotate=v))
        self.tracking = self._dspin(-0.1, 0.4, 0.01, lambda v: self._set(tracking=v))
        self.line_height = self._dspin(0.7, 2.0, 0.02, lambda v: self._set(line_height=v))
        self.text_width = self._dspin(0.2, 1.0, 0.02, lambda v: self._set(text_width=v))

        self.align = self._combo(ALIGNS, lambda v: self._set(align=v))
        self.position = self._combo(POSITIONS, lambda v: self._set(position=v))

        self.uppercase = QCheckBox("UPPERCASE")
        self.uppercase.toggled.connect(lambda v: self._set(uppercase=v))
        self.italic = QCheckBox("Italic")
        self.italic.toggled.connect(lambda v: self._set(italic=v))
        self.shadow = QCheckBox("Shadow")
        self.shadow.toggled.connect(lambda v: self._set(shadow=v))

        self.btn_colour = QPushButton()
        self.btn_colour.clicked.connect(lambda: self._pick("colour", "Text colour"))
        self.btn_outline = QPushButton()
        self.btn_outline.clicked.connect(lambda: self._pick("outline", "Outline colour"))
        self.btn_box = QPushButton()
        self.btn_box.clicked.connect(lambda: self._pick("box", "Slab behind the words"))
        self.btn_nobox = QPushButton("No slab")
        self.btn_nobox.clicked.connect(lambda: self._set(box=""))

        # ---- actions and preview -----------------------------------------
        self.btn_build = QPushButton("Build thumbnail")
        self.btn_build.clicked.connect(self.build_requested)
        self.btn_open = QPushButton("Open output")
        self.btn_open.clicked.connect(self._open_output)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.preview = QLabel("No thumbnail yet.")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(260)
        self.preview.setStyleSheet("border: 1px solid #3c4043; color: #9aa0a6;")

        self._lay_out()
        self._select(-1)

    # ---- construction helpers -------------------------------------------

    def _spin(self, low, high, suffix, slot, step=1) -> QSpinBox:
        box = QSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        if suffix:
            box.setSuffix(suffix)
        box.valueChanged.connect(slot)
        return box

    def _dspin(self, low, high, step, slot) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        box.setDecimals(2)
        box.valueChanged.connect(slot)
        return box

    def _combo(self, items, slot) -> QComboBox:
        box = QComboBox()
        box.addItems(items)
        box.currentTextChanged.connect(slot)
        return box

    def _lay_out(self) -> None:
        picture = QGroupBox("Picture")
        pv = QVBoxLayout(picture)
        pv.addWidget(QLabel("From the sheet's thumbnail column:"))
        pv.addWidget(self.description)
        row = QHBoxLayout()
        row.addWidget(self.btn_queue)
        row.addWidget(self.btn_suggest)
        row.addStretch(1)
        pv.addLayout(row)

        side = QVBoxLayout()
        side.addWidget(QLabel("Text blocks"))
        side.addWidget(self.list, 1)
        add = QHBoxLayout()
        add.addWidget(self.preset, 1)
        add.addWidget(self.btn_add)
        side.addLayout(add)
        tools = QHBoxLayout()
        for button in (self.btn_copy, self.btn_del, self.btn_up, self.btn_down):
            tools.addWidget(button)
        side.addLayout(tools)

        editor = QVBoxLayout()
        editor.addWidget(self.text)
        line1 = QHBoxLayout()
        for label, widget in (
            ("Font", self.font), ("Size", self.size), ("Weight", self.weight),
            ("Outline", self.outline_px),
        ):
            line1.addWidget(QLabel(label))
            line1.addWidget(widget)
        line1.addWidget(self.btn_colour)
        line1.addWidget(self.btn_outline)
        line1.addStretch(1)

        line2 = QHBoxLayout()
        for label, widget in (
            ("Align", self.align), ("Position", self.position),
            ("Nudge x", self.dx), ("y", self.dy), ("Rotate", self.rotate),
        ):
            line2.addWidget(QLabel(label))
            line2.addWidget(widget)
        line2.addStretch(1)

        line3 = QHBoxLayout()
        for label, widget in (
            ("Tracking", self.tracking), ("Line height", self.line_height),
            ("Max width", self.text_width), ("Slab pad", self.box_pad),
        ):
            line3.addWidget(QLabel(label))
            line3.addWidget(widget)
        line3.addWidget(self.btn_box)
        line3.addWidget(self.btn_nobox)
        line3.addWidget(self.uppercase)
        line3.addWidget(self.italic)
        line3.addWidget(self.shadow)
        line3.addStretch(1)

        for layout in (line1, line2, line3):
            editor.addLayout(layout)
        editor.addStretch(1)

        middle = QHBoxLayout()
        middle.addLayout(side)
        middle.addLayout(editor, 1)

        actions = QHBoxLayout()
        actions.addWidget(self.status, 1)
        actions.addWidget(self.btn_build)
        actions.addWidget(self.btn_open)

        root = QVBoxLayout(self)
        root.addWidget(picture)
        root.addLayout(middle)
        root.addLayout(actions)
        root.addWidget(self.preview, 1)

    # ---- editing ---------------------------------------------------------

    @property
    def current(self) -> int:
        return self.list.currentRow()

    def _set(self, **changes) -> None:
        """Apply a change to the selected block and publish the new list."""
        if self._loading or not (0 <= self.current < len(self.blocks)):
            return
        self.blocks[self.current] = replace(self.blocks[self.current], **changes)
        if "text" in changes:
            self._relabel(self.current)
        self._publish()

    def _publish(self) -> None:
        self.blocks_changed.emit([asdict(b) for b in self.blocks])

    def _relabel(self, index: int) -> None:
        block = self.blocks[index]
        first = (block.text.strip().splitlines() or [""])[0]
        item = self.list.item(index)
        if item is not None:
            item.setText(f"{index + 1}. {first[:22] or '(empty)'}")

    def _add(self) -> None:
        name = self.preset.currentText()
        self.blocks.append(TextBlock(text="New line", **PRESETS.get(name, {})))
        self._reload_list(select=len(self.blocks) - 1)
        self._publish()

    def _duplicate(self) -> None:
        if not (0 <= self.current < len(self.blocks)):
            return
        source = self.blocks[self.current]
        self.blocks.insert(self.current + 1, replace(source, dy=source.dy + 40))
        self._reload_list(select=self.current + 1)
        self._publish()

    def _remove(self) -> None:
        if not (0 <= self.current < len(self.blocks)):
            return
        index = self.current
        del self.blocks[index]
        self._reload_list(select=min(index, len(self.blocks) - 1))
        self._publish()

    def _move(self, step: int) -> None:
        index = self.current
        target = index + step
        if not (0 <= index < len(self.blocks)) or not (0 <= target < len(self.blocks)):
            return
        self.blocks[index], self.blocks[target] = self.blocks[target], self.blocks[index]
        self._reload_list(select=target)
        self._publish()

    def _pick(self, key: str, title: str) -> None:
        if not (0 <= self.current < len(self.blocks)):
            return
        current = getattr(self.blocks[self.current], key) or "#FFFFFF"
        chosen = QColorDialog.getColor(QColor(current), self, title)
        if chosen.isValid():
            self._set(**{key: chosen.name().upper()})
            self._paint()

    def _paint(self) -> None:
        if not (0 <= self.current < len(self.blocks)):
            for button, label in (
                (self.btn_colour, "Text"), (self.btn_outline, "Outline"),
                (self.btn_box, "Slab"),
            ):
                button.setText(label)
                button.setStyleSheet("")
            return
        block = self.blocks[self.current]
        for button, label, value in (
            (self.btn_colour, "Text", block.colour),
            (self.btn_outline, "Outline", block.outline),
            (self.btn_box, "Slab", block.box or "none"),
        ):
            button.setText(f"{label}  {value}")
            button.setStyleSheet(
                f"QPushButton {{ border-left: 14px solid {value}; padding-left: 6px; }}"
                if value.startswith("#")
                else ""
            )

    # ---- loading ---------------------------------------------------------

    def _reload_list(self, select: int = 0) -> None:
        self._loading = True
        self.list.clear()
        for index in range(len(self.blocks)):
            self.list.addItem("")
            self._relabel(index)
        self._loading = False
        if self.blocks:
            self.list.setCurrentRow(max(0, min(select, len(self.blocks) - 1)))
        else:
            self._select(-1)

    def _select(self, index: int) -> None:
        enabled = 0 <= index < len(self.blocks)
        for widget in (
            self.text, self.font, self.size, self.weight, self.outline_px,
            self.dx, self.dy, self.rotate, self.tracking, self.line_height,
            self.text_width, self.box_pad, self.align, self.position,
            self.uppercase, self.italic, self.shadow, self.btn_colour,
            self.btn_outline, self.btn_box, self.btn_nobox, self.btn_copy,
            self.btn_del, self.btn_up, self.btn_down,
        ):
            widget.setEnabled(enabled)
        if not enabled:
            self._paint()
            return

        block = self.blocks[index]
        self._loading = True
        self.text.setPlainText(block.text)
        self.font.setCurrentText(block.font)
        for widget, value in (
            (self.size, block.size), (self.weight, block.weight),
            (self.outline_px, block.outline_px), (self.dx, block.dx),
            (self.dy, block.dy), (self.box_pad, block.box_pad),
            (self.rotate, block.rotate), (self.tracking, block.tracking),
            (self.line_height, block.line_height),
            (self.text_width, block.text_width),
        ):
            widget.setValue(value)
        self.align.setCurrentText(block.align)
        self.position.setCurrentText(block.position)
        self.uppercase.setChecked(block.uppercase)
        self.italic.setChecked(block.italic)
        self.shadow.setChecked(block.shadow)
        self._loading = False
        self._paint()

    # ---- state -----------------------------------------------------------

    def set_project(self, project, cfg) -> None:
        self.project = project
        self.cfg = cfg
        self.blocks = list(cfg.thumbnail.blocks)
        self._reload_list()
        self.show_existing()

    def set_blocks(self, blocks) -> None:
        """Replace every block, as the sheet suggestion does."""
        self.blocks = list(blocks)
        self._reload_list()
        self._publish()

    def set_description(self, description: str, picture: Path | None) -> None:
        if not description:
            self.description.setText(
                f"nothing yet — add a '{self.cfg.sheets.thumbnail_column}' column "
                "to the sheet, describe the picture in it, then Source → Load"
            )
        else:
            self.description.setText(description)
        self.btn_queue.setEnabled(bool(description))
        self.btn_build.setEnabled(picture is not None)
        if description and picture is None:
            self.status.setText(
                "<span style='color:#d79a3a'>the picture is queued but not "
                "generated yet — press Start run</span>"
            )
        else:
            self.status.setText("")

    def show_existing(self) -> None:
        if self.project is None:
            return
        made = self.project.output_dir / "thumbnail.jpg"
        if made.exists():
            self.show_result(made)

    def show_result(self, path: Path) -> None:
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self.preview.setPixmap(
            pixmap.scaled(
                max(self.preview.width(), 480), max(self.preview.height(), 260),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        )
        self.preview.setText("")

    def _open_output(self) -> None:
        if self.project is not None:
            open_path(self.project.output_dir)
