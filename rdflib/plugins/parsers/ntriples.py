#!/usr/bin/env python3

__doc__ = """\
N-Triples Parser
License: GPL 2, W3C, BSD, or MIT
Author: Sean B. Palmer, inamidst.com
"""

import codecs
import logging
import re
from io import BytesIO, StringIO, TextIOBase
from typing import IO, TYPE_CHECKING, Optional, Pattern, TextIO, Union

from rdflib.compat import _string_escape_map, decodeUnicodeEscape
from rdflib.exceptions import ParserError as ParseError
from rdflib.parser import InputSource, Parser
from rdflib.term import BNode as bNode
from rdflib.term import Literal, Node
from rdflib.term import URIRef as URI

if TYPE_CHECKING:
    from rdflib.graph import Graph

__all__ = ["unquote", "uriquote", "W3CNTriplesParser", "NTGraphSink", "NTParser"]

uriref = r'<([^:]+:[^\s"<>]*)>'
literal = r'"([^"\\]*(?:\\.[^"\\]*)*)"'
litinfo = r"(?:@([a-zA-Z]+(?:-[a-zA-Z0-9]+)*)|\^\^" + uriref + r")?"

r_line = re.compile(r"([^\r\n]*)(?:\r\n|\r|\n)")
r_wspace = re.compile(r"[ \t]*")
r_wspaces = re.compile(r"[ \t]+")
r_tail = re.compile(r"[ \t]*\.[ \t]*(#.*)?")
r_uriref = re.compile(uriref)
r_nodeid = re.compile(r"_:([A-Za-z0-9_:]([-A-Za-z0-9_:\.]*[-A-Za-z0-9_:])?)")
r_literal = re.compile(literal + litinfo)

bufsiz = 2048
validate = False


class DummySink(object):
    def __init__(self):
        self.length = 0

    def triple(self, s, p, o):
        self.length += 1
        print(s, p, o)


r_safe = re.compile(r"([\x20\x21\x23-\x5B\x5D-\x7E]+)")
r_quot = re.compile(r"""\\([tbnrf"'\\])""")
r_uniquot = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def unquote(s: str) -> str:
    """Unquote an N-Triples string."""
    if not validate:
        if isinstance(s, str):  # nquads
            s = decodeUnicodeEscape(s)
        else:
            s = s.decode("unicode-escape")  # type: ignore[unreachable]

        return s
    else:
        result = []
        while s:
            m = r_safe.match(s)
            if m:
                s = s[m.end() :]
                result.append(m.group(1))
                continue

            m = r_quot.match(s)
            if m:
                s = s[2:]
                result.append(_string_escape_map[m.group(1)])
                continue

            m = r_uniquot.match(s)
            if m:
                s = s[m.end() :]
                u, U = m.groups()
                codepoint = int(u or U, 16)
                if codepoint > 0x10FFFF:
                    raise ParseError("Disallowed codepoint: %08X" % codepoint)
                result.append(chr(codepoint))
            elif s.startswith("\\"):
                raise ParseError("Illegal escape at: %s..." % s[:10])
            else:
                raise ParseError("Illegal literal character: %r" % s[0])
        return "".join(result)


r_hibyte = re.compile(r"([\x80-\xFF])")


def uriquote(uri):
    if not validate:
        return uri
    else:
        return r_hibyte.sub(lambda m: "%%%02X" % ord(m.group(1)), uri)


class W3CNTriplesParser(object):
    """An N-Triples Parser.
    This is a legacy-style Triples parser for NTriples provided by W3C
    Usage::

          p = NTriplesParser(sink=MySink())
          sink = p.parse(f) # file; use parsestring for a string

    To define a context in which blank node identifiers refer to the same blank node
    across instances of NTriplesParser, pass the same dict as `bnode_context` to each
    instance. By default, a new blank node context is created for each instance of
    `NTriplesParser`.
    """

    __slots__ = (
        "_bnode_ids",
        "sink",
        "buffer",
        "file",
        "line",
        'ignore_errors',
        'line_number',
        'current_line',
    )

    def __init__(
        self, sink: Optional[Union[DummySink, "NTGraphSink"]] = None, bnode_context=None
    ):
        if bnode_context is not None:
            self._bnode_ids = bnode_context
        else:
            self._bnode_ids = {}

        self.sink: Union[DummySink, "NTGraphSink"]
        if sink is not None:
            self.sink = sink
        else:
            self.sink = DummySink()

        self.buffer: Optional[str] = None
        self.file: Optional[Union[TextIO, codecs.StreamReader]] = None
        self.line: Optional[str] = ""
        self.ignore_errors = False

    def parse(
        self,
        f: Union[TextIO, IO[bytes], codecs.StreamReader],
        bnode_context=None,
        ignore_errors=False,
    ):

        """
        Parse f as an N-Triples file.

        :type f: :term:`file object`
        :param f: the N-Triples source
        :type bnode_context: `dict`, optional
        :param bnode_context: a dict mapping blank node identifiers (e.g., ``a`` in ``_:a``)
                              to `~rdflib.term.BNode` instances. An empty dict can be
                              passed in to define a distinct context for a given call to
                              `parse`.
        """

        if not hasattr(f, "read"):
            raise ParseError("Item to parse must be a file-like object.")

        if not hasattr(f, "encoding") and not hasattr(f, "charbuffer"):
            # someone still using a bytestream here?
            f = codecs.getreader("utf-8")(f)  # type: ignore[arg-type]

        self.ignore_errors = ignore_errors
        self.file = f  # type: ignore[assignment]
        self.buffer = ""
        self.line_number = 0
        while True:
            self.line = self.readline()
            self.line_number += 1
            self.current_line = self.line
            if self.line is None:
                break
            try:
                self.parseline(bnode_context=bnode_context)
            except ParseError:

                if ignore_errors:
                    # logging the invalid triples in the input file and ignoring them while creating the final graph
                    logging.warning(
                        "Invalid triple at line #{}: {}; IGNORED".format(
                            self.line_number, self.current_line
                        )
                    )
                    continue
                else:
                    raise ParseError("Invalid line: {}".format(self.current_line))
        return self.sink

    def parsestring(self, s: Union[bytes, bytearray, str], **kwargs):
        """Parse s as an N-Triples string."""
        if not isinstance(s, (str, bytes, bytearray)):
            raise ParseError("Item to parse must be a string instance.")
        f: Union[codecs.StreamReader, StringIO]
        if isinstance(s, (bytes, bytearray)):
            f = codecs.getreader("utf-8")(BytesIO(s))
        else:
            f = StringIO(s)
        self.parse(f, **kwargs)

    def readline(self):
        """Read an N-Triples line from buffered input."""
        # N-Triples lines end in either CRLF, CR, or LF
        # Therefore, we can't just use f.readline()
        if not self.buffer:
            buffer = self.file.read(bufsiz)
            if not buffer:
                return None
            self.buffer = buffer

        while True:
            m = r_line.match(self.buffer)
            if m:  # the more likely prospect
                self.buffer = self.buffer[m.end() :]
                return m.group(1)
            else:
                buffer = self.file.read(bufsiz)
                if not buffer and not self.buffer.isspace():
                    # Last line does not need to be terminated with a newline
                    buffer += "\n"
                elif not buffer:
                    return None
                self.buffer += buffer

    def parseline(self, bnode_context=None):
        self.eat(r_wspace)
        if (not self.line) or self.line.startswith("#"):
            return  # The line is empty or a comment

        subject = self.subject(bnode_context)
        self.eat(r_wspaces)

        predicate = self.predicate()
        self.eat(r_wspaces)

        object_ = self.object(bnode_context)

        if self.ignore_errors:
            if self.line == '':
                logging.warning(
                    "Invalid triple at line #{}: {} (There is no full stop); CORRECTED".format(
                        self.line_number, self.current_line
                    )
                )
            elif self.line != ".":
                logging.warning(
                    "Invalid triple at line #{}: {} (Extra characters detected at the end of triple); CORRECTED".format(
                        self.line_number, self.current_line
                    )
                )
            self.line = "."

        self.eat(r_tail)

        if self.line:
            raise ParseError("Trailing garbage: {}".format(self.line))
        self.sink.triple(subject, predicate, object_)

    def peek(self, token: str):
        return self.line.startswith(token)  # type: ignore[union-attr]

    def eat(self, pattern: Pattern[str]):
        m = pattern.match(self.line)  # type: ignore[arg-type]
        if not m:  # @@ Why can't we get the original pattern?
            # print(dir(pattern))
            raise ParseError("Failed to eat %s at %s" % (pattern.pattern, self.line))
        self.line = self.line[m.end() :]  # type: ignore[index]
        return m

    def subject(self, bnode_context=None):
        # @@ Consider using dictionary cases

        subj = self.uriref() or self.nodeid(bnode_context)
        if not subj:
            raise ParseError("Subject must be uriref or nodeID")
        return subj

    def predicate(self):
        pred = self.uriref()
        if not pred:
            raise ParseError("Predicate must be uriref")
        return pred

    def object(self, bnode_context=None):
        objt = self.uriref() or self.nodeid(bnode_context) or self.literal()
        if objt is False:
            raise ParseError("Unrecognised object type")
        return objt

    def uriref(self):
        if self.peek("<"):
            uri = self.eat(r_uriref).group(1)
            uri = unquote(uri)
            uri = uriquote(uri)
            return URI(uri)
        return False

    def nodeid(self, bnode_context=None):
        if self.peek("_"):
            # Fix for https://github.com/RDFLib/rdflib/issues/204
            if bnode_context is None:
                bnode_context = self._bnode_ids
            bnode_id = self.eat(r_nodeid).group(1)
            new_id = bnode_context.get(bnode_id, None)
            if new_id is not None:
                # Re-map to id specific to this doc
                return bNode(new_id)
            else:
                # Replace with freshly-generated document-specific BNode id
                bnode = bNode()
                # Store the mapping
                bnode_context[bnode_id] = bnode
                return bnode
        return False

    def literal(self):
        if self.peek('"'):
            lit, lang, dtype = self.eat(r_literal).groups()
            if lang:
                lang = lang
            else:
                lang = None
            if dtype:
                dtype = unquote(dtype)
                dtype = uriquote(dtype)
                dtype = URI(dtype)
            else:
                dtype = None
            if lang and dtype:
                raise ParseError("Can't have both a language and a datatype")
            lit = unquote(lit)
            return Literal(lit, lang, dtype)
        return False


class NTGraphSink(object):
    __slots__ = ("g",)

    def __init__(self, graph: "Graph"):
        self.g = graph

    def triple(self, s: Node, p: Node, o: Node):
        self.g.add((s, p, o))


class NTParser(Parser):
    """parser for the ntriples format, often stored with the .nt extension

    See http://www.w3.org/TR/rdf-testcases/#ntriples"""

    __slots__ = ()

    @classmethod
    def parse(cls, source: InputSource, sink: "Graph", **kwargs):
        """
        Parse the NT format

        :type source: `rdflib.parser.InputSource`
        :param source: the source of NT-formatted data
        :type sink: `rdflib.graph.Graph`
        :param sink: where to send parsed triples
        :param kwargs: Additional arguments to pass to `.NTriplesParser.parse`
        """
        f: Union[TextIO, IO[bytes], codecs.StreamReader]
        f = source.getCharacterStream()
        if not f:
            b = source.getByteStream()
            # TextIOBase includes: StringIO and TextIOWrapper
            if isinstance(b, TextIOBase):
                # f is not really a ByteStream, but a CharacterStream
                f = b  # type: ignore[assignment]
            else:
                # since N-Triples 1.1 files can and should be utf-8 encoded
                f = codecs.getreader("utf-8")(b)
        parser = W3CNTriplesParser(NTGraphSink(sink))
        parser.parse(f, **kwargs)
        f.close()
