def wrap(text, width):
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if isinstance(width, bool) or not isinstance(width, int):
        raise TypeError('width must be int')
    if width < 1:
        raise ValueError('width must be at least 1')
    lines = []
    current = ''
    for word in text.split(' '):
        if not current:
            current = word
        elif len(current) + 1 + len(word) < width:
            current += ' ' + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
