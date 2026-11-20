from __future__ import annotations

from dataclasses import dataclass

from fanfictl.auth import UserRecord
from fanfictl.keystore import SecretSummary, _SecretStore, mask_secret, secret_id_for

PixivTokenSummary = SecretSummary
pixiv_token_id_for = secret_id_for


def mask_pixiv_token(value: str) -> str:
    return mask_secret(value, 8)


@dataclass
class RuntimePixivToken:
    id: str
    refresh_token: str
    source: str
    is_default: bool


class PixivTokenStore(_SecretStore):
    table = "pixiv_tokens"
    value_column = "refresh_token"
    hash_column = "refresh_token_hash"
    mask_prefix_len = 8
    empty_error = "Pixiv refresh token cannot be empty"

    def _env_value(self) -> str:
        return self.settings.pixiv_refresh_token or ""

    def runtime_tokens_for_user(
        self, user: UserRecord | None
    ) -> list[RuntimePixivToken]:
        return [
            RuntimePixivToken(id=i, refresh_token=v, source=s, is_default=d)
            for i, v, s, d in self._runtime_entries(user)
        ]

    def list_personal_tokens(self, user: UserRecord) -> list[SecretSummary]:
        return self._personal_summaries(user)

    def list_global_tokens(self) -> list[SecretSummary]:
        return self._global_summaries()

    def add_user_token(self, user: UserRecord, raw_token: str) -> None:
        self._add(raw_token, owner_user_id=user.id, scope="user")

    def add_global_token(self, raw_token: str) -> None:
        self._add(raw_token, owner_user_id=None, scope="global")

    def remove_user_token(self, user: UserRecord, token_id: str) -> None:
        self._remove(token_id, owner_user_id=user.id, scope="user")

    def remove_global_token(self, token_id: str) -> None:
        self._remove(token_id, owner_user_id=None, scope="global")
