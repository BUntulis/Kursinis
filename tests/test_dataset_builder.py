"""Testai dataset extractor'iui."""
from __future__ import annotations

from pathlib import Path

from src.dataset.build_dataset import extract_pairs


SAMPLE = '''
def short():
    """Per trumpa."""
    return 1


def make_user(username, email):
    """Sukuria User objektą su username ir email reikšmėmis ir grąžina jį.

    Naudojama testų fiksūruose ir admin migracijose.
    """
    user = User.objects.create(username=username, email=email)
    user.set_password("changeme")
    user.save()
    return user


class BookViewSet:
    """ViewSet skirtas Book modeliui valdyti per DRF API.

    Suteikia standartinį CRUD funkcionalumą su autentifikacijos reikalavimu.
    """
    queryset = Book.objects.all()
    serializer_class = BookSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(owner=self.request.user)
'''


def test_extract_pairs_filters_short_and_keeps_documented(tmp_path: Path):
    f = tmp_path / "sample.py"
    f.write_text(SAMPLE, encoding="utf-8")

    pairs = list(extract_pairs(f))
    names = {p["_meta"]["name"] for p in pairs}

    assert "short" not in names  # docstring per trumpas
    assert "make_user" in names
    assert "BookViewSet" in names
    for p in pairs:
        assert "Django" in p["instruction"]
        assert p["output"].strip()
