"""Database routers for multi-db setups."""

class LegacySqliteRouter:
    """
    Second DB alias `legacy` is read-only SQLite used only for importing data.
    Never run migrations against it.
    """

    def db_for_read(self, model, **hints):
        return None

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == 'legacy':
            return False
        return None
