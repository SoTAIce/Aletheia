def parse_csv_line(line, sep=','):
    if not isinstance(line, str) or not isinstance(sep, str):
        raise TypeError('line and sep must be str')
    if len(sep) != 1 or sep in '"\r\n':
        raise ValueError('sep must be one character other than a quote or newline')
    fields = []
    i, n = 0, len(line)
    while True:
        chars = []
        if i < n and line[i] == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValueError('unterminated quote')
                if line[i] == '"':
                    if i + 1 < n and line[i + 1] == '"':
                        chars.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                chars.append(line[i])
                i += 1
            if i < n and line[i] != sep:
                raise ValueError('text after closing quote')
        else:
            while i < n and line[i] != sep:
                if line[i] in '"\r\n':
                    raise ValueError('quote or newline in unquoted field')
                chars.append(line[i])
                i += 1
        fields.append(''.join(chars))
        if i >= n:
            return fields
        i += 1
