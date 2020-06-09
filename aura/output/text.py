import re
from textwrap import shorten, wrap
from prettyprinter import pformat
from typing import Optional

from click import echo, secho, style

from .. import utils
from .. import config
from ..exceptions import MinimumScoreNotReached
from .base import AuraOutput, DiffOutputBase


# Reference for unicode box characters:
# https://jrgraphix.net/r/Unicode/2500-257F

class PrettyReport:
    ANSI_RE = re.compile(r"""
    (\x1b     # literal ESC
    \[       # literal [
    [;\d]*   # zero or more digits or semicolons
    [A-Za-z]) # a letter
    """, re.VERBOSE)

    def __init__(self):
        self.width = config.get_int("aura.text-output-width", 120)

    @classmethod
    def ansi_length(cls, line:str):
        return len(cls.ANSI_RE.sub("", line))

    def print_separator(self, sep="\u2504", left="\u251C", right="\u2524"):
        secho(f"{left}{sep*(self.width-2)}{right}")

    def print_top_separator(self):
        self.print_separator(left="\u2552", sep="\u2550", right="\u2555")

    def print_bottom_separator(self):
        self.print_separator(left="\u2558", sep="\u2550", right="\u255B")

    def print_heading(self, text, left="\u251C", right="\u2524", infill="\u2591"):
        text_len = self.ansi_length(text)
        ljust = (self.width-4-text_len)//2
        rjust = self.width-4-text_len-ljust
        secho(f"{left}{infill*ljust} {text} {infill*rjust}{right}")

    def align(self, line, pos=-1, left="\u2502 ", right=" \u2502"):
        content_len = self.ansi_length(line)
        remaining_len = self.width - len(left) - len(right)

        if content_len > remaining_len:
            parts = self.ANSI_RE.split(line)
            longest = max(filter(lambda x: not x.startswith(r"\x1b"), parts), key=len)
            for idx, p in enumerate(parts):
                if p == longest:
                    parts[idx] = p[:len(p)-len(left)-len(right)-6] + " [...]"  # TODO

            line = "".join(parts)

        if pos == -1:
            line = line + " "*(remaining_len-content_len)
        else:
            line = " "*(remaining_len-content_len) + line

        echo(f"{left}{line}{right}")

    def wrap(self, text, left="\u2502 ", right=" \u2502"):
        remaining_len=self.width - len(left) - len(right)
        for line in wrap(text, width=remaining_len):
            self.align(line, left=left, right=right)

    def pformat(self, obj, left="\u2502 ", right=" \u2502"):
        remaining_len = self.width - len(left) - len(right)
        for line in pformat(obj, width=remaining_len).splitlines(False):
            self.align(line, left=left, right=right)


class TextBase:
    formatter = PrettyReport()

    def _format_detection(
            self,
            hit,
            *,
            header: Optional[str]=None,
            top_separator=True,
            bottom_separator=True
    ):
        out = self.formatter
        if top_separator:
            out.print_top_separator()

        if header is None:
            header = style(hit["type"], "green", bold=True)
        out.align(header)

        out.print_separator()
        out.wrap(hit["message"])
        out.print_separator()

        if hit.get('line_no') or hit.get('location'):
            line_info = f"Line {style(str(hit.get('line_no', 'N/A')), 'blue', bold=True)}"
            line_info += f" at {style(hit['location'], 'blue', bold=True)}"
            out.align(line_info)

        if hit.get('line'):
            out.align(style(hit["line"], "cyan"))
        out.print_separator()

        score = f"Score: {style(str(hit['score']), 'blue', bold=True)}"
        if hit.get('informational'):
            score += ", informational"
        out.align(score)

        out.align(f"Tags: {', '.join(hit.get('tags', []))}")
        out.align("Extra:")
        out.pformat(hit.get('extra', {}))
        if bottom_separator:
            out.print_bottom_separator()

    def pprint_imports(self, tree, indent=""):
        """
        pretty print the module tree
        """
        last = len(tree) - 1
        for ix, x in enumerate(tree.keys()):
            subitems = tree.get(x, {})

            # https://en.wikipedia.org/wiki/Box-drawing_character
            char = ""
            if ix == last:
                char += "└"
            elif ix == 0:
                char += "┬"
            else:
                char += "├"

            yield f"{indent}{char} {style(x, fg='bright_blue')}"
            if subitems:
                new_indent = " " if ix == last else "│"
                yield from self.pprint_imports(subitems, indent + new_indent)

    def imports_to_tree(self, items: list) -> dict:
        """
        Transform a list of imported modules into a module tree
        """
        root = {}
        for x in items:
            parts = x.split(".")
            current = root
            for x in parts:
                if x not in current:
                    current[x] = {}
                current = current[x]

        return root


class TextOutput(AuraOutput, TextBase):
    formatter = PrettyReport()

    def output(self, hits):
        hits = set(hits)
        imported_modules = {h.extra["name"] for h in hits if h.name == "ModuleImport"}

        try:
            hits = self.filtered(hits)
        except MinimumScoreNotReached:
            return

        score = 0
        tags = set()

        for h in hits:
            score += h.score
            tags |= h.tags

        score = sum(x.score for x in hits)

        if score < self.metadata.get("min_score", 0):
            return

        secho("\n")  # Empty line for readability
        self.formatter.print_top_separator()
        self.formatter.print_heading(style(f"Scan results for {self.metadata.get('name', 'N/A')}", fg="bright_green"))
        score_color = "bright_green" if score == 0 else "bright_red"
        self.formatter.align(style(f"Scan score: {score}", fg=score_color, bold=True))

        if len(tags) > 0:
            self.formatter.align(f"Tags:")
            for t in tags:
                self.formatter.align(f" - {t}")

        if imported_modules:
            self.formatter.print_heading("Imported modules")
            for line in self.pprint_imports(self.imports_to_tree(imported_modules)):
                self.formatter.align(line)
        else:
            self.formatter.print_heading("No imported modules detected")

        if hits:
            self.formatter.print_heading("Code detections")
            for h in hits:
                self._format_detection(h._asdict())
        else:
            self.formatter.print_heading(style("No code detections has been triggered", fg="bright_green"))
            self.formatter.print_bottom_separator()


class TextDiffOutput(DiffOutputBase, TextBase):
    def output_diff(self, diffs):
        out = self.formatter

        for diff in diffs:
            out.print_separator(left="\u2552", sep="\u2550", right="\u2555")

            if diff.operation in ("M", "R"):
                op = "Modified" if diff.operation == "M" else "Renamed"
                out.align(style(f"{op} file. Similarity: {int(diff.similarity * 100)}%", fg="bright_red", bold=True))
                out.align(f"A Path: {style(diff.a_ref, fg='bright_blue')}")
                out.align(f"B Path: {style(diff.b_ref, fg='bright_blue')}")
            elif diff.operation == "A":
                out.align(style(f"File added.", fg="bright_yellow"))
                out.align(f"Path: {style(diff.b_ref, fg='bright_blue')}")
            elif diff.operation == "D":
                out.align(style(f"File removed", fg="green"))
                out.align(f"Path: {style(diff.a_ref, fg='bright_blue')}")

            if diff.diff:
                out.print_heading("START OF DIFF")

                for diff_line in diff.diff.splitlines():
                    if diff_line.startswith("@@"):
                        opts = {"fg": "bright_blue"}
                    elif diff_line.startswith("+"):
                        opts = {"fg": "bright_green"}
                    elif diff_line.startswith("-"):
                        opts = {"fg": "bright_red"}
                    else:
                        opts = {"fg": "bright_black"}

                    out.align(style(diff_line, **opts))

                out.print_heading("END OF DIFF")

            if diff.removed_detections or diff.new_detections:
                out.print_separator()

            if diff.removed_detections:
                out.print_heading(style("Removed detections for this file", fg="bright_yellow"))
                for x in diff.removed_detections:
                    out.print_separator()
                    x = x._asdict()
                    header = style(f"Removed: '{x['type']}'", fg="green", bold=True)
                    self._format_detection(x, header=header, bottom_separator=False, top_separator=False)

            if diff.new_detections:
                out.print_heading(style("New detections for this file", fg="bright_red"))
                for x in diff.new_detections:
                    out.print_separator()
                    x = x._asdict()
                    header = style(f"Added: '{x['type']}'", fg="red", bold=True)
                    self._format_detection(x, header=header, bottom_separator=False, top_separator=False)

            out.print_separator(left="\u2558", sep="\u2550", right="\u255B")
