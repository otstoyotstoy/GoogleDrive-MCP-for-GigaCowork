"""Главное правило: без подтверждения пользователя и без явного разрешения в настройках
коннектор не может ничего удалить, затереть или открыть посторонним."""
import base64

import safety
import server
from conftest import audit_lines, call
from fake_google import FOLDER, GDOC, GSHEET


def changed(world):
    return [c for c in world.calls]


# ---------------- Удаление ----------------

def test_delete_without_confirm_changes_nothing(world):
    fid = world.add("Договор.docx")
    r = call(server.delete_file, file_id=fid)
    assert r["needs_confirmation"] is True
    assert world.files[fid]["trashed"] is False
    assert changed(world) == []


def test_delete_with_confirm_goes_to_trash_and_restores(world):
    fid = world.add("Договор.docx")
    r = call(server.delete_file, file_id=fid, confirm=True)
    assert r["trashed"] is True and world.files[fid]["trashed"] is True
    assert ("DELETE_FOREVER", fid) not in world.calls
    call(server.restore_file, file_id=fid)
    assert world.files[fid]["trashed"] is False


def test_permanent_delete_blocked_by_default(world):
    fid = world.add("x.txt")
    world.files[fid]["trashed"] = True
    r = call(server.delete_file, file_id=fid, permanent=True, confirm=True)
    assert r["blocked"] is True
    assert fid in world.files


def test_permanent_delete_only_from_trash_even_when_allowed(world, monkeypatch):
    monkeypatch.setattr(safety, "ALLOW_PERMANENT_DELETE", True)
    fid = world.add("x.txt")
    r = call(server.delete_file, file_id=fid, permanent=True, confirm=True)
    assert r["blocked"] is True and fid in world.files
    world.files[fid]["trashed"] = True
    r = call(server.delete_file, file_id=fid, permanent=True)
    assert r["needs_confirmation"] is True and fid in world.files
    r = call(server.delete_file, file_id=fid, permanent=True, confirm=True)
    assert r["deleted"] is True and fid not in world.files


def test_folder_delete_blocked_by_default(world):
    folder = world.add("Клиенты", FOLDER)
    world.add("a.docx", parents=[folder])
    r = call(server.delete_file, file_id=folder, confirm=True)
    assert r["blocked"] is True
    assert world.files[folder]["trashed"] is False


def test_folder_delete_when_allowed_shows_contents_count(world, monkeypatch):
    monkeypatch.setattr(safety, "ALLOW_FOLDER_DELETE", True)
    folder = world.add("Клиенты", FOLDER)
    world.add("a.docx", parents=[folder])
    world.add("b.docx", parents=[folder])
    r = call(server.delete_file, file_id=folder)
    assert r["needs_confirmation"] and r["details"]["items_inside"] == 2


def test_cannot_delete_root_work_folder(world, monkeypatch):
    root = world.add("Рабочая", FOLDER)
    monkeypatch.setattr(safety, "ALLOWED_FOLDER_IDS", [root])
    monkeypatch.setattr(safety, "ALLOW_FOLDER_DELETE", True)
    r = call(server.delete_file, file_id=root, confirm=True)
    assert r["blocked"] is True


def test_no_trash_right_reported(world):
    fid = world.add("чужой.docx")
    world.files[fid]["capabilities"]["canTrash"] = False
    r = call(server.delete_file, file_id=fid, confirm=True)
    assert r["blocked"] is True and world.files[fid]["trashed"] is False


# ---------------- Режим только чтения ----------------

def test_read_only_blocks_every_write(world, monkeypatch):
    monkeypatch.setattr(safety, "READ_ONLY", True)
    fid = world.add("a.txt")
    sid = world.add("Т", GSHEET)
    did = world.add("Д", GDOC)
    results = [
        call(server.upload_file, name="n.txt", content="x"),
        call(server.delete_file, file_id=fid, confirm=True),
        call(server.rename_file, file_id=fid, new_name="b"),
        call(server.update_file_content, file_id=fid, content="y", confirm=True),
        call(server.sheets_write, spreadsheet_id=sid, range="A1", values=[[1]], confirm=True),
        call(server.sheets_append, spreadsheet_id=sid, range="Лист1", values=[[1]]),
        call(server.docs_append_text, document_id=did, text="t"),
        call(server.share_file, file_id=fid, email="a@b.ru", confirm=True),
        call(server.create_folder, name="x"),
    ]
    assert all(r.get("blocked") for r in results), results
    assert world.calls == []
    assert "files" in call(server.list_files)


# ---------------- Перезапись ----------------

def test_google_doc_and_sheet_never_overwritten_whole(world):
    for mime in (GDOC, GSHEET):
        fid = world.add("Важное", mime)
        r = call(server.update_file_content, file_id=fid, content="a,b\n1,2", confirm=True)
        assert r["blocked"] is True
    assert world.calls == []


def test_overwrite_binary_needs_confirm_and_keeps_old_revision(world):
    fid = world.add("отчёт.pdf", "application/pdf")
    r = call(server.update_file_content, file_id=fid, content=base64.b64encode(b"%PDF").decode(), is_base64=True)
    assert r["needs_confirmation"] and world.calls == []
    r = call(server.update_file_content, file_id=fid, content=base64.b64encode(b"%PDF").decode(), is_base64=True, confirm=True)
    assert r["updated"] and r["previous_revision_kept"] == "rev1"
    assert world.kept_revisions == [(fid, "rev1", {"keepForever": True})]


def test_overwrite_disabled_setting(world, monkeypatch):
    monkeypatch.setattr(safety, "ALLOW_OVERWRITE", False)
    fid = world.add("a.txt")
    assert call(server.update_file_content, file_id=fid, content="x", confirm=True)["blocked"]


def test_bad_base64_rejected(world):
    fid = world.add("a.pdf", "application/pdf")
    r = call(server.update_file_content, file_id=fid, content="не base64!!", is_base64=True, confirm=True)
    assert r["error"] and world.calls == []


# ---------------- Доступ ----------------

def test_public_link_sharing_blocked(world):
    fid = world.add("Финансы.xlsx")
    r = call(server.share_file, file_id=fid, anyone_with_link=True, confirm=True)
    assert r["blocked"] is True and world.calls == []


def test_ownership_transfer_blocked(world):
    fid = world.add("a.txt")
    assert call(server.share_file, file_id=fid, email="x@y.ru", role="owner", confirm=True)["blocked"]


def test_share_needs_confirm(world):
    fid = world.add("a.txt")
    r = call(server.share_file, file_id=fid, email="x@y.ru", role="writer")
    assert r["needs_confirmation"] and world.calls == []
    r = call(server.share_file, file_id=fid, email="x@y.ru", role="writer", confirm=True)
    assert r["shared"] is True


def test_cannot_revoke_owner_and_revoke_needs_confirm(world):
    fid = world.add("a.txt")
    assert call(server.remove_permission, file_id=fid, permission_id="p_owner", confirm=True)["blocked"]
    call(server.share_file, file_id=fid, email="x@y.ru", confirm=True)
    pid = world.perms[fid][-1]["id"]
    r = call(server.remove_permission, file_id=fid, permission_id=pid)
    assert r["needs_confirmation"] and len(world.perms[fid]) == 2
    call(server.remove_permission, file_id=fid, permission_id=pid, confirm=True)
    assert len(world.perms[fid]) == 1


# ---------------- Google Таблицы ----------------

def test_sheets_write_to_empty_cells_no_confirm_needed(world):
    sid = world.add("Сделки", GSHEET)
    r = call(server.sheets_write, spreadsheet_id=sid, range="'Лист1'!A1", values=[["Имя", "Сумма"], ["Иван", 1000]])
    assert r["updatedCells"] == 4 and r["overwritten_non_empty_cells"] == 0


def test_sheets_write_over_data_requires_confirm_and_backs_up(world):
    sid = world.add("Сделки", GSHEET)
    world.sheets[sid]["tabs"]["Лист1"][(2, 2)] = "=SUM(A1:A5)"
    r = call(server.sheets_write, spreadsheet_id=sid, range="'Лист1'!A1", values=[["a", "b"], ["c", "d"]])
    assert r["needs_confirmation"] and r["details"]["non_empty_cells_to_overwrite"] == 1
    assert world.sheets[sid]["tabs"]["Лист1"][(2, 2)] == "=SUM(A1:A5)"
    r = call(server.sheets_write, spreadsheet_id=sid, range="'Лист1'!A1", values=[["a", "b"], ["c", "d"]], confirm=True)
    assert r["overwritten_non_empty_cells"] == 1
    backups = [x for x in audit_lines() if x["status"] == "backup" and x["file_id"] == sid]
    assert backups and "=SUM(A1:A5)" in backups[-1]["previous_values"]


def test_sheets_write_detects_formula_errors(world):
    sid = world.add("Сделки", GSHEET)
    r = call(server.sheets_write, spreadsheet_id=sid, range="'Лист1'!A1", values=[["=ROUND(1,2)"]])
    assert r["formula_errors"] and "';'" in r["hint"]


def test_sheets_write_too_many_cells(world):
    sid = world.add("Т", GSHEET)
    big = [[1] * 100 for _ in range(safety.MAX_SHEET_CELLS // 100 + 1)]
    assert call(server.sheets_write, spreadsheet_id=sid, range="A1", values=big)["blocked"]


def test_sheets_clear_requires_range_and_confirm(world):
    sid = world.add("Т", GSHEET)
    world.sheets[sid]["tabs"]["Лист1"][(1, 1)] = "важно"
    assert call(server.sheets_clear, spreadsheet_id=sid, range="'Лист1'", confirm=True)["blocked"]
    r = call(server.sheets_clear, spreadsheet_id=sid, range="'Лист1'!A1:B2")
    assert r["needs_confirmation"] and world.sheets[sid]["tabs"]["Лист1"][(1, 1)] == "важно"
    call(server.sheets_clear, spreadsheet_id=sid, range="'Лист1'!A1:B2", confirm=True)
    assert (1, 1) not in world.sheets[sid]["tabs"]["Лист1"]


def test_sheets_append_never_overwrites(world):
    sid = world.add("Т", GSHEET)
    world.sheets[sid]["tabs"]["Лист1"][(1, 1)] = "шапка"
    call(server.sheets_append, spreadsheet_id=sid, range="'Лист1'", values=[["новая"]])
    assert world.sheets[sid]["tabs"]["Лист1"][(1, 1)] == "шапка"
    assert world.sheets[sid]["tabs"]["Лист1"][(2, 1)] == "новая"


def test_sheet_tools_reject_non_sheet(world):
    fid = world.add("data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    r = call(server.sheets_write, spreadsheet_id=fid, range="A1", values=[[1]])
    assert r["error"] and world.calls == []


# ---------------- Google Docs ----------------

def test_docs_template_fill(world):
    did = world.add("Договор", GDOC)
    world.docs[did]["text"] = "Заказчик: {{client}}. Цена: {{price}}."
    r = call(server.docs_replace_many, document_id=did, replacements={"{{client}}": "ООО Ромашка", "{{price}}": "100", "{{nope}}": "x"})
    assert world.docs[did]["text"] == "Заказчик: ООО Ромашка. Цена: 100."
    assert r["not_found"] == ["{{nope}}"]


def test_docs_short_or_empty_find_rejected(world):
    did = world.add("Д", GDOC)
    world.docs[did]["text"] = "абв"
    for f in ("", " ", "а"):
        r = call(server.docs_replace_text, document_id=did, find=f, replace_with="x", confirm=True)
        assert r["error"]
    assert world.docs[did]["text"] == "абв"


def test_docs_delete_text_requires_confirm(world):
    did = world.add("Д", GDOC)
    world.docs[did]["text"] = "Пункт 5. Штраф 1 млн."
    r = call(server.docs_replace_text, document_id=did, find="Штраф 1 млн.", replace_with="")
    assert r["needs_confirmation"] and "Штраф" in world.docs[did]["text"]


def test_docs_mass_replace_requires_confirm(world):
    did = world.add("Д", GDOC)
    world.docs[did]["text"] = "срок " * 50
    r = call(server.docs_replace_text, document_id=did, find="срок", replace_with="период")
    assert r["needs_confirmation"] and "период" not in world.docs[did]["text"]
    r = call(server.docs_replace_text, document_id=did, find="срок", replace_with="период", confirm=True)
    assert r["replaced_count"] == 50


def test_docs_insert_missing_anchor_changes_nothing(world):
    did = world.add("Д", GDOC)
    world.docs[did]["text"] = "текст"
    r = call(server.docs_insert_text, document_id=did, text="!", after_text="нет такого")
    assert r["error"] and world.docs[did]["text"] == "текст"


def test_docs_tools_reject_non_doc(world):
    fid = world.add("x.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert call(server.docs_append_text, document_id=fid, text="t")["error"]
    assert world.calls == []


# ---------------- Ограничение папками ----------------

def test_scope_restriction(world, monkeypatch):
    allowed = world.add("Рабочая", FOLDER)
    sub = world.add("Подпапка", FOLDER, parents=[allowed])
    inside = world.add("внутри.txt", parents=[sub])
    outside = world.add("снаружи.txt")
    monkeypatch.setattr(safety, "ALLOWED_FOLDER_IDS", [allowed])
    assert "file" in call(server.get_file_info, file_id=inside) or call(server.get_file_info, file_id=inside).get("id")
    assert call(server.get_file_info, file_id=outside)["blocked"]
    assert call(server.rename_file, file_id=outside, new_name="x")["blocked"]
    assert call(server.move_file, file_id=inside, new_folder_id="root")["blocked"]
    assert call(server.upload_file, name="a.txt", content="x", folder_id="root")["blocked"]
    names = [f["name"] for f in call(server.search_files, name_contains=".txt")["files"]]
    assert "внутри.txt" in names and "снаружи.txt" not in names
    assert world.files[outside]["name"] == "снаружи.txt"


# ---------------- Журнал ----------------

def test_audit_log_records_changes_and_blocks(world):
    fid = world.add("a.txt")
    call(server.rename_file, file_id=fid, new_name="b.txt")
    call(server.share_file, file_id=fid, anyone_with_link=True, confirm=True)
    lines = audit_lines()
    assert any(x["tool"] == "rename_file" and x["status"] == "ok" and x["old_name"] == "a.txt" for x in lines)
    assert any(x["tool"] == "share_file" and x["status"] == "blocked" for x in lines)
