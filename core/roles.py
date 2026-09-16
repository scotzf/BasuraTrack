"""
Who is allowed to see which of the three interfaces.

Sukod has three separate front doors, and they are separate on purpose:

    /          the notebook   - janitor      - records, sees nothing else
    /monitor/  the dashboard  - supervisor   - monitors, records nothing
    /manage/   the back room  - admin        - creates the other two

The separation is not decoration. Two of the rules in CLAUDE.md depend on it:

  * The purity check is NOT the janitor's job. If it appears on their screen
    they will eventually be asked to do it, and a purity check done in a hurry
    by the person whose sacks are being judged is worthless. Removing it from
    their navigation is how that stays true.

  * Logged volume must never become a performance measure for the janitor.
    The janitor's interface therefore has no dashboard, no comparison between
    areas, and no way to see totals. They cannot see a number that could be
    held against them, because the screen does not render one.

Two different identity systems meet here. Read `staff_for` below for why.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def staff_for(request):
    """
    Return the Staff badge for the signed-in user, or None.

    Bootstrap rule: a Django superuser who has no Staff row is treated as an
    admin. Without this the very first `manage.py createsuperuser` would lock
    itself out of the screen that creates Staff rows - you would need a Staff
    row to reach the page that makes Staff rows. The superuser flag is the
    only credential that exists before any of our own data does.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None

    staff = getattr(user, "staff", None)
    if staff is not None:
        return staff

    if user.is_superuser:
        # A stand-in, deliberately NOT saved to the database. Creating rows as
        # a side effect of reading a page is the kind of surprise that makes
        # an audit trail untrustworthy. The admin screen offers to create the
        # real row explicitly.
        from .models import Site, Staff
        return Staff(user=user, site=Site.objects.first(), role="admin")

    return None


def _guard(view, allowed_roles):
    """
    Shared body of the two decorators below.

    Not signed in  -> send to the login page, remembering where they wanted to
                      go, so they land on it after signing in.
    Signed in but
    wrong role     -> 403. Deliberately not a redirect: bouncing a supervisor
                      who typed /manage/ back to their dashboard looks like a
                      bug, while "you do not have access" is the truth.
    """
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        staff = staff_for(request)
        if staff is None:
            return redirect_to_login(request.get_full_path())
        if staff.role not in allowed_roles:
            raise PermissionDenied(
                "This screen is for %s only." % " or ".join(allowed_roles))
        # Stash it so the view and its template need not look it up again.
        request.staff = staff
        return view(request, *args, **kwargs)
    return wrapped


def supervisor_required(view):
    """
    Monitoring screens: dashboard, report, purity check, purity history.

    Admins are allowed through as well. An admin who could create supervisor
    accounts but could not open the dashboard would be a strange kind of
    superior, and in a small campus team the admin IS often the supervisor.
    """
    return _guard(view, {"supervisor", "admin"})


def admin_required(view):
    """
    The back room: creating accounts, pairing devices, calibration, site setup.

    Supervisors are refused. Calibration is the number every mass in the
    system is multiplied by, so the set of people who can change it is kept
    as small as the set of people who can create accounts.
    """
    return _guard(view, {"admin"})
