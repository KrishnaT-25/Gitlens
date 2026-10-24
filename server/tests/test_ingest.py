import io
import zipfile
import pytest
from app.errors import AppError
from app.services.ingest import parse_archive

def archive(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for path, content in files.items():
            z.writestr(path, content)
    return out.getvalue()

def test_line_mapping_and_filtering():
    text = "\n".join(f"line {n}" for n in range(1, 201))
    result = parse_archive(archive({"root/src/main.py": text, "root/node_modules/x.js": "ignored", "root/.env": "SECRET=abc"}))
    assert [(c.line_start, c.line_end) for c in result] == [(1, 80), (71, 150), (141, 200)]
    assert all(c.path == "src/main.py" for c in result)

def test_traversal_rejected():
    with pytest.raises(AppError) as error:
        parse_archive(archive({"root/../evil.py": "bad"}))
    assert error.value.code == "unsafe_archive"

def test_empty_repository_truthful():
    with pytest.raises(AppError) as error:
        parse_archive(archive({"root/logo.png": b"\0\1\2"}))
    assert error.value.code == "empty_repository"

def test_detectable_secret_skipped():
    result = parse_archive(archive({"root/secret.py": "-----BEGIN PRIVATE KEY-----", "root/app.py": "print('safe')"}))
    assert [c.path for c in result] == ["app.py"]
