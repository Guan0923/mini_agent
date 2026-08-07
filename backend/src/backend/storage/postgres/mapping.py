"""PostgreSQL row and conversation projection helpers."""

from backend.domain import RunStatus, Session, SessionSummary

from ..codec import assistant_content, normalize_session_title


class SessionMappingMixin:
    @staticmethod
    def _clean_title(title: str | None) -> str:
        return normalize_session_title(title)

    @staticmethod
    def _assistant_content(status: RunStatus, answer: str | None) -> str:
        return assistant_content(status, answer)

    @staticmethod
    def _session_from_row(row: tuple[object, ...]) -> Session:
        return Session(
            session_id=str(row[0]),
            title=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
            client_id=str(row[4]) if row[4] is not None else None,
            archived_at=str(row[5]) if row[5] is not None else None,
            deleted_at=str(row[6]) if row[6] is not None else None,
        )

    @staticmethod
    def _summary_from_row(row: tuple[object, ...]) -> SessionSummary:
        # Accept pre-tree rows from embedders that still use the old summary
        # query while the database adapter is being upgraded.
        has_nodes = len(row) >= 11
        return SessionSummary(
            session_id=str(row[0]),
            title=str(row[1]),
            created_at=str(row[2]),
            updated_at=str(row[3]),
            message_count=int(row[4]),
            last_node_id=(str(row[5]) if row[5] is not None else None) if has_nodes else None,
            last_run_id=str(row[6 if has_nodes else 5]) if row[6 if has_nodes else 5] is not None else None,
            last_run_status=str(row[7 if has_nodes else 6]) if row[7 if has_nodes else 6] is not None else None,
            client_id=str(row[8 if has_nodes else 7]) if row[8 if has_nodes else 7] is not None else None,
            archived_at=str(row[9 if has_nodes else 8]) if row[9 if has_nodes else 8] is not None else None,
            deleted_at=str(row[10 if has_nodes else 9]) if row[10 if has_nodes else 9] is not None else None,
        )

    @staticmethod
    def _summary_query(where: str) -> str:
        return f"""
            SELECT
                s.session_id,
                s.title,
                s.created_at,
                s.updated_at,
                (
                    SELECT CASE WHEN EXISTS (
                        SELECT 1 FROM runtime_nodes AS existing_node
                        WHERE existing_node.session_id = s.session_id
                    ) THEN (
                        SELECT COUNT(*) FROM runtime_nodes AS node_count
                        WHERE node_count.session_id = s.session_id
                    ) ELSE (
                        SELECT COUNT(*) FROM session_messages AS legacy_message
                        WHERE legacy_message.session_id = s.session_id
                    ) END
                ) AS message_count,
                (
                    SELECT leaf.id FROM runtime_nodes AS leaf
                    WHERE leaf.session_id = s.session_id
                    AND NOT EXISTS (
                        SELECT 1 FROM runtime_nodes AS child
                        WHERE child.parent_session_id = leaf.session_id AND child.parent_id = leaf.id
                    )
                    ORDER BY leaf.timestamp DESC, leaf.id DESC LIMIT 1
                ) AS last_node_id,
                r.run_id AS last_run_id,
                r.status AS last_run_status,
                s.client_id,
                s.archived_at,
                s.deleted_at
            FROM sessions AS s
            LEFT JOIN session_runs AS r ON r.run_id = (
                SELECT latest.run_id
                FROM session_runs AS latest
                WHERE latest.session_id = s.session_id
                ORDER BY latest.updated_at DESC, latest.run_id DESC
                LIMIT 1
            )
            {where}
            GROUP BY s.session_id, s.title, s.created_at, s.updated_at, r.run_id, r.status,
                s.client_id, s.archived_at, s.deleted_at
        """
