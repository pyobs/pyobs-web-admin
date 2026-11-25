"""The detached restart job started by services.restart_module(), see services.run_restart()."""

from django.core.management.base import BaseCommand, CommandError

from modules import services


class Command(BaseCommand):
    help = "Restarts a module: stops it, waits until it is gone (e.g. a camera warming up), starts it again."

    def add_arguments(self, parser):
        parser.add_argument("name", type=str)

    def handle(self, *args, **options):
        ok, msg = services.run_restart(options["name"])
        if not ok:
            raise CommandError(msg)
        self.stdout.write(msg)
