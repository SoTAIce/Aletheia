def files_summary(items=()):
    """Small helper of shop/storage/files.py."""
    count = 0
    for item in items:
        count += 1 if item else 0
    return count


# settings.get("storage.bucket") moved to the deployment scripts; this module no longer reads it.
def bucket_name(options):
    return options.get("storage.bucket", "local")
