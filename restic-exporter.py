#!/usr/bin/env python3
import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector


class ResticCollector(Collector):
    def __init__(
        self,
        repository,
        password_file,
        exit_on_error,
        disable_check,
        disable_stats,
        disable_locks,
        include_paths,
        insecure_tls,
    ):
        self.repository = repository
        self.password_file = password_file
        self.exit_on_error = exit_on_error
        self.disable_check = disable_check
        self.disable_stats = disable_stats
        self.disable_locks = disable_locks
        self.include_paths = include_paths
        self.insecure_tls = insecure_tls
        # todo: the stats cache increases over time -> remove old ids
        # todo: cold start -> the stats cache could be saved in a persistent volume
        # todo: cold start -> the restic cache (/root/.cache/restic) could be
        # saved in a persistent volume
        self.stats_cache = {}
        self.metrics = {}
        self.refresh(exit_on_error)

    def collect(self):
        logging.debug("Incoming request")

        common_label_names = [
            "client_hostname",
            "client_username",
            "client_version",
            "snapshot_hash",
            "snapshot_tag",
            "snapshot_tags",
            "snapshot_paths",
        ]

        check_success = GaugeMetricFamily(
            "restic_check_success",
            "Result of restic check operation in the repository",
            labels=[],
        )
        locks_total = CounterMetricFamily(
            "restic_locks_total",
            "Total number of locks in the repository",
            labels=[],
        )
        snapshots_total = CounterMetricFamily(
            "restic_snapshots_total",
            "Total number of snapshots in the repository",
            labels=[],
        )
        backup_timestamp = GaugeMetricFamily(
            "restic_backup_timestamp",
            "Timestamp of the last backup",
            labels=common_label_names,
        )
        backup_files_total = CounterMetricFamily(
            "restic_backup_files_total",
            "Number of files in the backup",
            labels=common_label_names,
        )
        backup_size_total = CounterMetricFamily(
            "restic_backup_size_total",
            "Total size of backup in bytes",
            labels=common_label_names,
        )
        backup_snapshots_total = CounterMetricFamily(
            "restic_backup_snapshots_total",
            "Total number of snapshots",
            labels=common_label_names,
        )
        backup_files_new = GaugeMetricFamily(
            "restic_backup_files_new",
            "Number of new files in the backup",
            labels=common_label_names
        )
        backup_files_changed = GaugeMetricFamily(
            "restic_backup_files_changed",
            "Number of changed files in the backup",
            labels=common_label_names
        )
        backup_files_unmodified = GaugeMetricFamily(
            "restic_backup_files_unmodified",
            "Number of unmodified files in the backup",
            labels=common_label_names
        )
        backup_files_processed = GaugeMetricFamily(
            "restic_backup_files_processed",
            "Number of files processed in the last backup",
            labels=common_label_names
        )
        backup_bytes_processed = GaugeMetricFamily(
            "restic_backup_bytes_processed",
            "Number of bytes processed in the last backup",
            labels=common_label_names
        )
        backup_bytes_added = GaugeMetricFamily(
            "restic_backup_bytes_added",
            "Number of bytes added in the last backup",
            labels=common_label_names
        )
        backup_duration_seconds = GaugeMetricFamily(
            "restic_backup_duration_seconds",
            "Amount of time each backup takes",
            labels=common_label_names
        )
        scrape_duration_seconds = GaugeMetricFamily(
            "restic_scrape_duration_seconds",
            "Amount of time each scrape takes",
            labels=[],
        )

        check_success.add_metric([], self.metrics["check_success"])
        locks_total.add_metric([], self.metrics["locks_total"])
        snapshots_total.add_metric([], self.metrics["snapshots_total"])

        for client in self.metrics["clients"]:
            common_label_values = [
                client["hostname"],
                client["username"],
                client["version"],
                client["snapshot_hash"],
                client["snapshot_tag"],
                client["snapshot_tags"],
                client["snapshot_paths"],
            ]

            backup_timestamp.add_metric(
                common_label_values, client["timestamp"])
            backup_files_total.add_metric(
                common_label_values, client["files_total"])
            backup_size_total.add_metric(
                common_label_values, client["size_total"])
            backup_snapshots_total.add_metric(
                common_label_values, client["snapshots_total"]
            )

            # Snapshot specific metrics
            backup_files_new.add_metric(
                common_label_values, client["files_new"]
            )
            backup_files_changed.add_metric(
                common_label_values, client["files_changed"]
            )
            backup_files_unmodified.add_metric(
                common_label_values, client["files_unmodified"]
            )
            backup_files_processed.add_metric(
                common_label_values, client["total_files_processed"]
            )
            backup_bytes_processed.add_metric(
                common_label_values, client["total_bytes_processed"]
            )
            backup_bytes_added.add_metric(
                common_label_values, client["total_bytes_added"]
            )

            # Scrape duration
            backup_duration_seconds.add_metric(
                common_label_values, client["duration_seconds"]
            )

        scrape_duration_seconds.add_metric([], self.metrics["duration"])

        yield check_success
        yield locks_total
        yield snapshots_total
        yield backup_timestamp
        yield backup_files_total
        yield backup_size_total
        yield backup_snapshots_total
        yield backup_duration_seconds
        yield backup_files_new
        yield backup_files_changed
        yield backup_files_unmodified
        yield backup_files_processed
        yield backup_bytes_processed
        yield backup_bytes_added
        yield scrape_duration_seconds

    def refresh(self, exit_on_error=False):
        try:
            self.metrics = self.get_metrics()
        except Exception:
            logging.error(
                "Unable to collect metrics from Restic. %s",
                traceback.format_exc(0).replace("\n", " "),
            )

            # Shutdown exporter for any error
            if exit_on_error:
                sys.exit(1)

    def get_metrics(self):
        duration = time.time()

        # calc total number of snapshots per hash
        all_snapshots = self.get_snapshots()
        snap_total_counter = {}
        for snap in all_snapshots:
            if snap["hash"] not in snap_total_counter:
                snap_total_counter[snap["hash"]] = 1
            else:
                snap_total_counter[snap["hash"]] += 1

        # get the latest snapshot per hash
        latest_snapshots_dup = self.get_snapshots(True)
        latest_snapshots = {}
        for snap in latest_snapshots_dup:
            timestamp = time.mktime(
                datetime.datetime.fromisoformat(snap["time"]).timetuple())
            snap["timestamp"] = timestamp
            if (
                snap["hash"] not in latest_snapshots
                or snap["timestamp"] > latest_snapshots[snap["hash"]]["timestamp"]
            ):
                latest_snapshots[snap["hash"]] = snap

        clients = []
        for snap in list(latest_snapshots.values()):
            # collect stats for each snap only if enabled
            if self.disable_stats:
                # return zero as "no-stats" value
                stats = {
                    "total_size": -1,
                    "total_file_count": -1,
                }
            else:
                stats = self.get_stats(snap["id"])

            clients.append(
                {
                    "hostname": snap["hostname"],
                    "username": snap["username"],
                    "version": (
                        snap["program_version"] if "program_version" in snap else ""
                    ),
                    "snapshot_hash": snap["hash"],
                    "snapshot_tag": snap["tags"][0] if "tags" in snap else "",
                    "snapshot_tags": ",".join(snap["tags"]) if "tags" in snap else "",
                    "snapshot_paths": (
                        ",".join(snap["paths"]) if self.include_paths else ""
                    ),
                    "timestamp": snap["timestamp"],
                    "size_total": stats["total_size"],
                    "files_total": stats["total_file_count"],
                    "files_new": self.get_summary(snap, "files_new"),
                    "files_changed": self.get_summary(snap, "files_changed"),
                    "files_unmodified": self.get_summary(snap, "files_unmodified"),
                    "total_files_processed": self.get_summary(
                        snap, "total_files_processed"
                    ),
                    "total_bytes_processed": self.get_summary(
                        snap, "total_bytes_processed"
                    ),
                    "total_bytes_added": self.get_summary(
                        snap, "data_added"
                    ),
                    "snapshots_total": snap_total_counter[snap["hash"]],
                    "duration_seconds": self.calc_duration(snap)
                }
            )

        # todo: fix the commented code when the bug is fixed in restic
        #  https://github.com/restic/restic/issues/2126
        # stats = self.get_stats()

        if self.disable_check:
            # return 2 as "no-check" value
            check_success = 2
        else:
            check_success = self.get_check()

        if self.disable_locks:
            # return 0 as "no-locks" value
            locks_total = 0
        else:
            locks_total = self.get_locks()

        metrics = {
            "check_success": check_success,
            "locks_total": locks_total,
            "clients": clients,
            "snapshots_total": len(all_snapshots),
            "duration": time.time() - duration,
            # 'size_total': stats['total_size'],
            # 'files_total': stats['total_file_count'],
        }

        return metrics

    def get_snapshots(self, only_latest=False):
        cmd = [
            "restic",
            "-r",
            self.repository,
            "-p",
            self.password_file,
            "--no-lock",
            "snapshots",
            "--json",
        ]

        if only_latest:
            cmd.extend(["--latest", "1"])

        if self.insecure_tls:
            cmd.extend(["--insecure-tls"])

        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise Exception(
                "Error executing restic snapshot command: " +
                self.parse_stderr(result)
            )
        snapshots = json.loads(result.stdout.decode("utf-8"))
        for snap in snapshots:
            if "username" not in snap:
                snap["username"] = ""
            snap["hash"] = self.calc_snapshot_hash(snap)
        return snapshots

    def get_stats(self, snapshot_id=None):
        # This command is expensive in CPU/Memory (1-5 seconds),
        # and much more when snapshot_id=None (3 minutes) -> we avoid this call for now
        # https://github.com/restic/restic/issues/2126
        if snapshot_id is not None and snapshot_id in self.stats_cache:
            return self.stats_cache[snapshot_id]

        cmd = [
            "restic",
            "-r",
            self.repository,
            "-p",
            self.password_file,
            "--no-lock",
            "stats",
            "--json",
        ]
        if snapshot_id is not None:
            cmd.extend([snapshot_id])

        if self.insecure_tls:
            cmd.extend(["--insecure-tls"])

        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise Exception(
                "Error executing restic stats command: " +
                self.parse_stderr(result)
            )
        stats = json.loads(result.stdout.decode("utf-8"))

        if snapshot_id is not None:
            self.stats_cache[snapshot_id] = stats

        return stats

    def get_check(self):
        # This command takes 20 seconds or more, but it's required
        cmd = [
            "restic",
            "-r",
            self.repository,
            "-p",
            self.password_file,
            "--no-lock",
            "check",
        ]

        if self.insecure_tls:
            cmd.extend(["--insecure-tls"])

        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode == 0:
            return 1  # ok
        else:
            logging.warning(
                "Error checking the repository health. " +
                self.parse_stderr(result)
            )
            return 0  # error

    def get_locks(self):
        cmd = [
            "restic",
            "-r",
            self.repository,
            "-p",
            self.password_file,
            "--no-lock",
            "list",
            "locks",
        ]

        if self.insecure_tls:
            cmd.extend(["--insecure-tls"])

        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise Exception(
                "Error executing restic list locks command: "
                + self.parse_stderr(result)
            )
        text_result = result.stdout.decode("utf-8")
        lock_counter = 0
        for line in text_result.split("\n"):
            if re.match("^[a-z0-9]+$", line):
                lock_counter += 1

        return lock_counter

    def get_summary(self, snap, metric):
        if "summary" in snap:
            return snap["summary"].get(metric, 0)
        return 0

    @staticmethod
    def calc_snapshot_hash(snapshot: dict) -> str:
        text = snapshot["hostname"] + \
            snapshot["username"] + ",".join(snapshot["paths"])
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def calc_duration(self, snapshot: dict) -> float:
        backup_start = snapshot.get('summary', {}).get('backup_start')
        backup_end = snapshot.get('summary', {}).get('backup_end')
        if not backup_start or not backup_end:
            return 0.0
        try:
            start_time = datetime.datetime.fromisoformat(backup_start)
            end_time = datetime.datetime.fromisoformat(backup_end)
            duration = end_time - start_time
            return duration.total_seconds()
        except Exception as e:
            logging.warning(f"Could not parse backup_start or backup_end: {e}")
            return 0.0

    @staticmethod
    def parse_stderr(result):
        return (
            result.stderr.decode("utf-8").replace("\n", " ")
            + " Exit code: "
            + str(result.returncode)
        )


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO")),
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logging.info("Starting Restic Prometheus Exporter")
    logging.info("It could take a while if the repository is remote")

    restic_repo_url = os.environ.get("RESTIC_REPOSITORY")
    if restic_repo_url is None:
        restic_repo_url = os.environ.get("RESTIC_REPO_URL")
        if restic_repo_url is not None:
            logging.warning(
                "The environment variable RESTIC_REPO_URL is deprecated, "
                "please use RESTIC_REPOSITORY instead."
            )
    if restic_repo_url is None:
        logging.error(
            "The environment variable RESTIC_REPOSITORY is mandatory")
        sys.exit(1)

    restic_repo_password_file = os.environ.get("RESTIC_PASSWORD_FILE")
    if restic_repo_password_file is None:
        restic_repo_password_file = os.environ.get("RESTIC_REPO_PASSWORD_FILE")
        if restic_repo_password_file is not None:
            logging.warning(
                "The environment variable RESTIC_REPO_PASSWORD_FILE is deprecated, "
                "please use RESTIC_PASSWORD_FILE instead."
            )
    if restic_repo_password_file is None:
        logging.error(
            "The environment variable RESTIC_PASSWORD_FILE is mandatory")
        sys.exit(1)

    exporter_address = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
    exporter_port = int(os.environ.get("LISTEN_PORT", 8001))
    exporter_refresh_interval = int(os.environ.get("REFRESH_INTERVAL", 60))
    exporter_exit_on_error = bool(os.environ.get("EXIT_ON_ERROR", False))
    exporter_disable_check = bool(os.environ.get("NO_CHECK", False))
    exporter_disable_stats = bool(os.environ.get("NO_STATS", False))
    exporter_disable_locks = bool(os.environ.get("NO_LOCKS", False))
    exporter_include_paths = bool(os.environ.get("INCLUDE_PATHS", False))
    exporter_insecure_tls = bool(os.environ.get("INSECURE_TLS", False))

    try:
        collector = ResticCollector(
            restic_repo_url,
            restic_repo_password_file,
            exporter_exit_on_error,
            exporter_disable_check,
            exporter_disable_stats,
            exporter_disable_locks,
            exporter_include_paths,
            exporter_insecure_tls,
        )
        REGISTRY.register(collector)
        start_http_server(exporter_port, exporter_address)
        logging.info(
            "Serving at http://{0}:{1}".format(exporter_address, exporter_port)
        )

        while True:
            logging.info(
                "Refreshing stats every {0} seconds".format(
                    exporter_refresh_interval)
            )
            time.sleep(exporter_refresh_interval)
            collector.refresh()

    except KeyboardInterrupt:
        logging.info("\nInterrupted")
        exit(0)
