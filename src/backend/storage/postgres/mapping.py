"""PostgreSQL row and conversation projection helpers."""

from backend.domain import DEFAULT_SESSION_TITLE, RunStatus, Session, SessionSummary


class SessionMappingMixin:
    @staticmethod
    def _clean_title(title: str | None) -> str:
        value = " ".join((title or "").split())
        return value[:80] or DEFAULT_SESSION_TITLE

    @staticmethod
    def _assistant_content(status: RunStatus, answer: str | None) -> str:
        if status == "completed":
            return answer or ""
        if status == "cancelled":
            return "Task cancelled by user."
        return answer or f"Task {status}."

    @staticmethod
    def _session_from_row(row: tuple[object, ...]) -> Session:
        return Session(
            session_id=str(row[0]),
            title=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
        )

    @staticmethod
    def _summary_from_row(row: tuple[object, ...]) -> SessionSummary:
        return SessionSummary(
            session_id=str(row[0]),
            title=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
            message_count=int(row[4]),
            last_run_id=str(row[5]) if row[5] is not None else None,
            last_run_status=str(row[6]) if row[6] is not None else None,
        )

    @staticmethod
    def _summary_query(where: str) -> str:
        return f"""
            SELECT
                s.session_id,
                s.title,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count,
                r.run_id AS last_run_id,
                r.status AS last_run_status
            FROM sessions AS s
            LEFT JOIN session_messages AS m ON m.session_id = s.session_id
            LEFT JOIN session_runs AS r ON r.run_id = (
                SELECT latest.run_id
                FROM session_runs AS latest
                WHERE latest.session_id = s.session_id
                ORDER BY latest.updated_at DESC, latest.run_id DESC
                LIMIT 1
            )
            {where}
            GROUP BY s.session_id, s.title, s.created_at, s.updated_at, r.run_id, r.status
        """
