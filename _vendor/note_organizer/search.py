"""Small Boolean query parser. User text is never evaluated as Python or SQL."""
import re


# PDF quotations may preserve a line break or join extracted words with one
# space. Both represent the same discretionary word break. Ordinary inline
# compounds ("tantalum-based") and numeric minus signs remain literal.
_WRAPPED_HYPHEN = re.compile(r"(?<=[^\W\d_])[-\u2010\u00ad]\s+(?=[^\W\d_])")


class SearchSyntaxError(ValueError):
    pass


def normalize(text):
    value = _WRAPPED_HYPHEN.sub("", str(text)).replace("\u00ad", "")
    return " ".join(value.casefold().split())


def tokenize(query):
    tokens, i = [], 0
    while i < len(query):
        if query[i].isspace():
            i += 1
            continue
        char = query[i]
        if char in "()":
            tokens.append((char, char))
            i += 1
        elif char == '"':
            i += 1
            phrase = []
            while i < len(query) and query[i] != '"':
                if query[i] == "\\" and i+1 < len(query) and query[i+1] in {'"', "\\"}:
                    i += 1
                phrase.append(query[i])
                i += 1
            if i == len(query):
                raise SearchSyntaxError('Close the quoted phrase with a double quote (").')
            i += 1
            phrase = normalize("".join(phrase))
            if not phrase:
                raise SearchSyntaxError("Put some text inside the quotes.")
            tokens.append(("TERM", phrase))
        else:
            start = i
            while i < len(query) and not query[i].isspace() and query[i] not in '()"':
                i += 1
            word = query[start:i]
            tokens.append((word, word) if word in {"AND", "OR", "NOT"} else ("TERM", normalize(word)))
        if len(tokens) > 256:
            raise SearchSyntaxError("This search is too long. Use fewer terms.")
    return tokens


def parse_search(query, mode="All words"):
    if len(query) > 4096:
        raise SearchSyntaxError("This search is too long. Use fewer terms.")
    if not query.strip():
        return None
    if mode == "Exact phrase":
        phrase = normalize(query)
        return ("TERM", phrase)
    tokens = tokenize(query)
    position = 0

    def peek():
        return tokens[position][0] if position < len(tokens) else "END"

    def unary(depth=0):
        nonlocal position
        if depth > 32:
            raise SearchSyntaxError("Too many nested parentheses or NOT operators.")
        kind = peek()
        if kind == "NOT":
            position += 1
            return ("NOT", unary(depth+1))
        if kind == "(":
            position += 1
            result = disjunction(depth+1)
            if peek() != ")":
                raise SearchSyntaxError("Close the parenthesis with ).")
            position += 1
            return result
        if kind != "TERM":
            raise SearchSyntaxError("Expected a word, quoted phrase, or group after the operator.")
        result = tokens[position]
        position += 1
        return result

    def conjunction(depth):
        nonlocal position
        left = unary(depth)
        while peek() in {"AND", "TERM", "(", "NOT"}:
            if peek() == "AND":
                position += 1
            left = ("AND", left, unary(depth))
        return left

    def disjunction(depth):
        nonlocal position
        left = conjunction(depth)
        while peek() == "OR":
            position += 1
            left = ("OR", left, conjunction(depth))
        return left

    expression = disjunction(0)
    if position != len(tokens):
        raise SearchSyntaxError("Unexpected closing parenthesis. Check the groups in your search.")
    return expression


def positive_terms(query, mode="All words"):
    """Literal terms to mark; NOT inverts polarity, including nested groups."""
    terms = []
    def visit(node, positive=True):
        if node is None:
            return
        if node[0] == "TERM":
            if positive and node[1] not in terms:
                terms.append(node[1])
        elif node[0] == "NOT":
            visit(node[1], not positive)
        else:
            visit(node[1], positive)
            visit(node[2], positive)
    visit(parse_search(query, mode))
    return terms


def match_spans(text, terms):
    """Map normalized matches, including wrapped words, to original offsets.

    The match covers both printed halves of a hyphenated word, retaining the
    intervening hyphen/whitespace in its original span. Filtering, counts and
    navigation therefore use the same normalized text without rewriting notes.
    """
    text = str(text)
    normalized, offsets = [], []
    breaks = iter(_WRAPPED_HYPHEN.finditer(text))
    current_break = next(breaks, None)
    for index, char in enumerate(text):
        if current_break is not None and index >= current_break.end():
            current_break = next(breaks, None)
        if char == "\u00ad" or (current_break is not None and current_break.start() <= index < current_break.end()):
            continue
        folded = " " if char.isspace() else char.casefold()
        if folded == " " and (not normalized or normalized[-1] == " "):
            continue
        normalized.extend(folded)
        offsets.extend([index] * len(folded))
    value = "".join(normalized)
    spans = set()
    for term in dict.fromkeys(normalize(term) for term in terms):
        start = 0
        while term:
            found = value.find(term, start)
            if found < 0:
                break
            spans.add((offsets[found], offsets[found + len(term) - 1] + 1))
            start = found + 1
    return sorted(spans)


def compile_search(query, mode="All words"):
    expression = parse_search(query, mode)
    if expression is None:
        return lambda values: True

    def evaluate(node, values):
        op = node[0]
        if op == "TERM":
            return any(node[1] in value for value in values)
        if op == "NOT":
            return not evaluate(node[1], values)
        if op == "AND":
            return evaluate(node[1], values) and evaluate(node[2], values)
        return evaluate(node[1], values) or evaluate(node[2], values)

    return lambda values: evaluate(expression, values)
