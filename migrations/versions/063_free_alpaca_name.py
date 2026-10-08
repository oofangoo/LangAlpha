"""Free the ``alpaca`` name for the connector shipped with it.

The pass ``030_free_brokerage_names``, ``032_free_moomoo_name`` and
``037_free_webull_name`` did for their own vendors, done here for one more. The
reasoning is unchanged and is not repeated: a brokerage's name IS its identity,
every surface joins a row to the shipped definition by name and then draws it
wearing that vendor, and the write paths that now refuse the name cannot repair
a row that was already sitting on it.

Why a fourth migration rather than an edit to any of them: each has run
everywhere, so amending one repairs no database that holds such a row, and a
migration records what the schema needed on the day it ran. That is why each
pins its names as literals instead of importing ``BROKERAGES``.

This one differs from 037 in having no address exemption. Webull published an
MCP endpoint people were adding by hand, so a row named ``webull`` at the
vendor's host could already be the connector and had to be left alone. Alpaca's
connector is served by a sidecar the operator deploys, at an address that did
not exist before this release, so no row that already holds the name can be it:
every one is somebody's own server, and every one is renamed. ``alpaca`` is an
ordinary word for a user to have picked, since Alpaca's own MCP server is
something people run themselves.

There is no workspace-tier half either, unlike 030, 032 and 037. Migration 055
moved every workspace-local server up to the user tier and deleted them, and left
a trigger that refuses any write to a ``source='workspace'`` row, so there is no
such row to rename and an UPDATE that found one would abort the whole upgrade.
Only the ``source='user'`` tombstones, which that trigger leaves alone, still
carry a name.

Revision ID: 063
Revises: 062
"""

from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None

# Pinned rather than imported, for the reason 030 gives: BROKERAGES is free to
# grow after this runs, and a later entry is that entry's migration to write.
_RESERVED = "('alpaca')"


def upgrade() -> None:
    # --- user tier -------------------------------------------------------
    # The scratch tables carry this revision in their names because ON COMMIT
    # DROP means "at the end of the transaction", and alembic runs the whole
    # upgrade in ONE transaction. A database coming from an earlier revision
    # still holds 030's, 032's and 037's identically-shaped tables when this
    # runs, so a bare name aborts every fresh install with "relation already
    # exists".
    #
    # The suffix is checked free against all three tables this rename writes a
    # name into, each of which has its own UNIQUE over it. The OAuth connection
    # is the one that can be holding a name nothing else holds, so it gets its
    # own check rather than riding on the server row's.
    op.execute(f"""
        CREATE TEMP TABLE mcp_user_renames_063 ON COMMIT DROP AS
        SELECT u.user_id,
               u.name AS old_name,
               CASE WHEN EXISTS (
                        SELECT 1 FROM user_mcp_servers x
                         WHERE x.user_id = u.user_id
                           AND x.name = u.name || '_legacy'
                    ) OR EXISTS (
                        SELECT 1
                          FROM workspace_mcp_servers w
                          JOIN workspaces ws USING (workspace_id)
                         WHERE ws.user_id = u.user_id
                           AND w.name = u.name || '_legacy'
                    ) OR EXISTS (
                        SELECT 1 FROM user_mcp_oauth_connections c
                         WHERE c.user_id = u.user_id
                           AND c.server_name = u.name || '_legacy'
                    )
                    THEN u.name || '_legacy_'
                         || replace(u.user_mcp_server_id::text, '-', '')
                    ELSE u.name || '_legacy'
               END AS new_name
          FROM user_mcp_servers u
         WHERE u.name IN {_RESERVED}
    """)

    op.execute("""
        UPDATE user_mcp_servers u SET name = r.new_name
          FROM mcp_user_renames_063 r
         WHERE u.user_id = r.user_id AND u.name = r.old_name
    """)
    # Moves with the row or the freed name inherits a live grant and the page
    # draws somebody else's server as a connected broker.
    op.execute("""
        UPDATE user_mcp_oauth_connections c SET server_name = r.new_name
          FROM mcp_user_renames_063 r
         WHERE c.user_id = r.user_id AND c.server_name = r.old_name
    """)
    op.execute("""
        UPDATE user_mcp_tool_schemas s SET server_name = r.new_name
          FROM mcp_user_renames_063 r
         WHERE s.user_id = r.user_id AND s.server_name = r.old_name
    """)
    # source='user' rows are tombstones naming an inherited user row that is
    # switched off in this workspace. Left behind, one would switch off the
    # brokerage the user connects later, in a workspace they never chose it for.
    op.execute("""
        UPDATE workspace_mcp_servers w SET name = r.new_name
          FROM mcp_user_renames_063 r, workspaces ws
         WHERE w.workspace_id = ws.workspace_id
           AND ws.user_id = r.user_id
           AND w.source = 'user'
           AND w.name = r.old_name
    """)

    # Every workspace whose effective set just changed, so a session holding the
    # old version re-resolves instead of running the renamed row under its old
    # name until something else happens to bump it.
    op.execute("""
        UPDATE workspaces ws SET mcp_config_version = ws.mcp_config_version + 1
         WHERE ws.user_id IN (SELECT user_id FROM mcp_user_renames_063)
    """)


def downgrade() -> None:
    # Deliberately empty, as 030's, 032's and 037's are. A renamed row is
    # indistinguishable from one a user named that way, and restoring the
    # reserved name would re-create the collision this exists to clear, on a
    # schema that still refuses it.
    pass
