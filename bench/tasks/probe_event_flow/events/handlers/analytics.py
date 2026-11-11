from events.bus import emit, on


@on("mail.sent")
def count_mail(payload):
    return "count_mail"


@on("points.awarded")
def count_points(payload):
    return "count_points"
