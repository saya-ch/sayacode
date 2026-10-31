"""笔记渲染。"""


def render(notes):
    return "\n\n".join("# " + note.title + "\n" + note.body for note in notes)
