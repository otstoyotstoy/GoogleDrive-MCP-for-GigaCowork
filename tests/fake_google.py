"""Имитация Google Drive / Docs / Sheets в памяти — для тестов без доступа к настоящему диску клиента."""
import copy
import itertools
import re

from googleapiclient.errors import HttpError

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"


class _Resp(dict):
    def __init__(self, status):
        super().__init__(status=str(status))
        self.status = status
        self.reason = "fake"


def http_error(status, msg):
    return HttpError(_Resp(status), ('{"error":{"message":"%s"}}' % msg).encode())


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class World:
    """Состояние «диска» + журнал всех изменяющих вызовов."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.files = {}
        self.perms = {}
        self.calls = []          # (метод, file_id) — что реально меняли
        self.sheets = {}         # sid -> {"locale":..., "tabs": {title: {(r,c): value}}}
        self.docs = {}           # did -> {"text": str, "rev": int}
        self.kept_revisions = []

    def add(self, name, mime="text/plain", parents=("root",), **kw):
        fid = kw.pop("id", None) or f"f{next(self.ids)}"
        self.files[fid] = dict(id=fid, name=name, mimeType=mime, parents=list(parents), trashed=False,
                               size="10", owners=[{"emailAddress": "owner@example.com"}],
                               capabilities={"canEdit": True, "canTrash": True, "canDelete": True, "canShare": True},
                               headRevisionId="rev1", **kw)
        self.perms[fid] = [{"id": "p_owner", "type": "user", "role": "owner", "emailAddress": "owner@example.com"}]
        if mime == GSHEET:
            self.sheets[fid] = {"locale": "ru_RU", "tabs": {"Лист1": {}}}
        if mime == GDOC:
            self.docs[fid] = {"text": "", "rev": 1}
        return fid


# ---------------- Drive ----------------

class Drive:
    def __init__(self, w: World):
        self.w = w

    def files(self):
        return _Files(self.w)

    def revisions(self):
        w = self.w

        class R:
            def update(self, fileId, revisionId, body):
                def f():
                    w.kept_revisions.append((fileId, revisionId, body))
                    return {"id": revisionId, "keepForever": True}
                return Req(f)
        return R()

    def permissions(self):
        return _Perms(self.w)

    def about(self):
        class A:
            def get(self, fields):
                return Req(lambda: {"user": {"displayName": "Test", "emailAddress": "owner@example.com"},
                                    "storageQuota": {"limit": "1000", "usage": "10"}})
        return A()

    def drives(self):
        class D:
            def list(self, **kw):
                return Req(lambda: {"drives": []})
        return D()


class _Files:
    def __init__(self, w):
        self.w = w

    def _get(self, fid):
        if fid not in self.w.files:
            raise http_error(404, "File not found")
        return self.w.files[fid]

    def get(self, fileId, fields=None, **kw):
        return Req(lambda: copy.deepcopy(self._get(fileId)))

    def list(self, q="", pageSize=100, **kw):
        def f():
            res = []
            m = re.search(r"'([^']+)' in parents", q)
            for x in self.w.files.values():
                if m and m.group(1) not in x["parents"]:
                    continue
                if "trashed = false" in q and x["trashed"]:
                    continue
                n = re.search(r"name contains '([^']+)'", q)
                if n and n.group(1) not in x["name"]:
                    continue
                res.append(copy.deepcopy(x))
            return {"files": res[:pageSize]}
        return Req(f)

    def create(self, body, media_body=None, fields=None, **kw):
        def f():
            fid = self.w.add(body["name"], body.get("mimeType", getattr(media_body, "mimetype", lambda: "text/plain")()),
                             parents=body.get("parents", ["root"]))
            self.w.calls.append(("create", fid))
            return copy.deepcopy(self.w.files[fid])
        return Req(f)

    def update(self, fileId, body=None, media_body=None, addParents=None, removeParents=None, fields=None, **kw):
        def f():
            x = self._get(fileId)
            if body:
                x.update(body)
            if addParents:
                x["parents"] = [p for p in x["parents"] if p not in (removeParents or "").split(",")] + [addParents]
            if media_body is not None:
                x["headRevisionId"] = "rev2"
                self.w.calls.append(("overwrite", fileId))
            if body and body.get("trashed"):
                self.w.calls.append(("trash", fileId))
            self.w.calls.append(("update", fileId))
            return copy.deepcopy(x)
        return Req(f)

    def copy(self, fileId, body, fields=None, **kw):
        def f():
            src = self._get(fileId)
            fid = self.w.add(body.get("name", "Копия " + src["name"]), src["mimeType"], parents=body.get("parents", src["parents"]))
            self.w.calls.append(("copy", fid))
            return copy.deepcopy(self.w.files[fid])
        return Req(f)

    def delete(self, fileId, **kw):
        def f():
            self._get(fileId)
            del self.w.files[fileId]
            self.w.calls.append(("DELETE_FOREVER", fileId))
            return ""
        return Req(f)

    def export(self, fileId, mimeType):
        return Req(lambda: b"exported")


class _Perms:
    def __init__(self, w):
        self.w = w

    def create(self, fileId, body, fields=None, **kw):
        def f():
            p = dict(body, id=f"p{len(self.w.perms[fileId])}")
            self.w.perms[fileId].append(p)
            self.w.calls.append(("share", fileId))
            return p
        return Req(f)

    def get(self, fileId, permissionId, fields=None, **kw):
        return Req(lambda: next(p for p in self.w.perms[fileId] if p["id"] == permissionId))

    def list(self, fileId, fields=None, **kw):
        return Req(lambda: {"permissions": self.w.perms[fileId]})

    def delete(self, fileId, permissionId, **kw):
        def f():
            self.w.perms[fileId] = [p for p in self.w.perms[fileId] if p["id"] != permissionId]
            self.w.calls.append(("revoke", fileId))
            return ""
        return Req(f)


# ---------------- Sheets ----------------

def _col(s):
    n = 0
    for ch in s:
        n = n * 26 + ord(ch.upper()) - 64
    return n


def _ncol(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def parse_range(rng, default_tab="Лист1"):
    tab, _, cells = rng.rpartition("!")
    tab = tab.strip("'") if tab else None
    if not cells or not re.match(r"^[A-Za-z]+\d*(:[A-Za-z]+\d*)?$", cells):
        tab, cells = (tab or rng.strip("'")), "A1:Z1000"
    a, _, b = cells.partition(":")
    ma = re.match(r"([A-Za-z]+)(\d*)", a)
    c1, r1 = _col(ma.group(1)), int(ma.group(2) or 1)
    if b:
        mb = re.match(r"([A-Za-z]+)(\d*)", b)
        c2, r2 = _col(mb.group(1)), int(mb.group(2) or 1000)
    else:
        c2, r2 = c1, r1
    return tab or default_tab, r1, c1, r2, c2


class Sheets:
    def __init__(self, w):
        self.w = w

    def spreadsheets(self):
        return _SS(self.w)


class _SS:
    def __init__(self, w):
        self.w = w

    def get(self, spreadsheetId, fields=None):
        def f():
            s = self.w.sheets[spreadsheetId]
            return {"properties": {"title": self.w.files[spreadsheetId]["name"], "locale": s["locale"]},
                    "sheets": [{"properties": {"sheetId": i, "title": t, "gridProperties": {"rowCount": 1000, "columnCount": 26}}}
                               for i, t in enumerate(s["tabs"])]}
        return Req(f)

    def batchUpdate(self, spreadsheetId, body):
        def f():
            replies = []
            for r in body["requests"]:
                if "addSheet" in r:
                    t = r["addSheet"]["properties"]["title"]
                    self.w.sheets[spreadsheetId]["tabs"][t] = {}
                    replies.append({"addSheet": {"properties": {"title": t}}})
                elif "updateSheetProperties" in r:
                    tabs = self.w.sheets[spreadsheetId]["tabs"]
                    old = list(tabs)[r["updateSheetProperties"]["properties"]["sheetId"]]
                    tabs[r["updateSheetProperties"]["properties"]["title"]] = tabs.pop(old)
                    replies.append({})
            return {"replies": replies}
        return Req(f)

    def values(self):
        return _Vals(self.w)


class _Vals:
    def __init__(self, w):
        self.w = w

    def _grid(self, sid, tab):
        return self.w.sheets[sid]["tabs"].setdefault(tab, {})

    def _fmt(self, tab, r1, c1, r2, c2):
        return f"'{tab}'!{_ncol(c1)}{r1}" + (f":{_ncol(c2)}{r2}" if (r1, c1) != (r2, c2) else "")

    def get(self, spreadsheetId, range, valueRenderOption="FORMATTED_VALUE"):
        def f():
            tab, r1, c1, r2, c2 = parse_range(range)
            g = self._grid(spreadsheetId, tab)
            rows = []
            for r in range_(r1, r2):
                row = [self._render(g.get((r, c), ""), valueRenderOption) for c in range_(c1, c2)]
                while row and row[-1] == "":
                    row.pop()
                rows.append(row)
            while rows and not rows[-1]:
                rows.pop()
            res = {"range": self._fmt(tab, r1, c1, r2, c2)}
            if rows:
                res["values"] = rows
            return res
        return Req(f)

    @staticmethod
    def _render(v, mode):
        if mode != "FORMULA" and isinstance(v, str) and v.startswith("="):
            return "#ERROR!" if "," in v and ";" not in v else "42"
        return v

    def batchGet(self, spreadsheetId, ranges, valueRenderOption="FORMATTED_VALUE"):
        return Req(lambda: {"valueRanges": [self.get(spreadsheetId, r, valueRenderOption).execute() for r in ranges]})

    def update(self, spreadsheetId, range, valueInputOption, body):
        def f():
            tab, r1, c1, _, _ = parse_range(range)
            g = self._grid(spreadsheetId, tab)
            for i, row in enumerate(body["values"]):
                for j, v in enumerate(row):
                    g[(r1 + i, c1 + j)] = v
            self.w.calls.append(("sheet_write", spreadsheetId))
            rows, cols = len(body["values"]), max(len(r) for r in body["values"])
            return {"updatedRange": self._fmt(tab, r1, c1, r1 + rows - 1, c1 + cols - 1), "updatedCells": rows * cols}
        return Req(f)

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        def f():
            tab = parse_range(range)[0]
            g = self._grid(spreadsheetId, tab)
            last = max([r for (r, _c) in g] or [0])
            for i, row in enumerate(body["values"]):
                for j, v in enumerate(row):
                    g[(last + 1 + i, 1 + j)] = v
            self.w.calls.append(("sheet_append", spreadsheetId))
            rows, cols = len(body["values"]), max(len(r) for r in body["values"])
            return {"updates": {"updatedRange": self._fmt(tab, last + 1, 1, last + rows, cols)}}
        return Req(f)

    def clear(self, spreadsheetId, range, body):
        def f():
            tab, r1, c1, r2, c2 = parse_range(range)
            g = self._grid(spreadsheetId, tab)
            for k in [k for k in g if r1 <= k[0] <= r2 and c1 <= k[1] <= c2]:
                del g[k]
            self.w.calls.append(("sheet_clear", spreadsheetId))
            return {"clearedRange": range}
        return Req(f)


def range_(a, b):
    return range(a, b + 1)


# ---------------- Docs ----------------

class Docs:
    def __init__(self, w):
        self.w = w

    def documents(self):
        w = self.w

        class D:
            def get(self, documentId):
                def f():
                    d = w.docs[documentId]
                    return {"revisionId": f"r{d['rev']}", "body": {"content": [
                        {"endIndex": 1},
                        {"endIndex": len(d["text"]) + 2, "paragraph": {"elements": [
                            {"startIndex": 1, "textRun": {"content": d["text"] + "\n"}}]}}]}}
                return Req(f)

            def batchUpdate(self, documentId, body):
                def f():
                    d = w.docs[documentId]
                    req_rev = body.get("writeControl", {}).get("requiredRevisionId")
                    if req_rev and req_rev != f"r{d['rev']}":
                        raise http_error(400, "Document was modified")
                    replies = []
                    for r in body["requests"]:
                        if "replaceAllText" in r:
                            k = r["replaceAllText"]["containsText"]["text"]
                            n = d["text"].count(k)
                            d["text"] = d["text"].replace(k, r["replaceAllText"]["replaceText"])
                            replies.append({"replaceAllText": {"occurrencesChanged": n}})
                        elif "insertText" in r:
                            i = r["insertText"]["location"]["index"] - 1
                            d["text"] = d["text"][:i] + r["insertText"]["text"] + d["text"][i:]
                            replies.append({})
                    d["rev"] += 1
                    w.calls.append(("doc_edit", documentId))
                    return {"replies": replies}
                return Req(f)
        return D()
