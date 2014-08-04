#!/usr/bin/env python
# This Source Code is subject to the terms of the Mozilla Public License
# version 2.0 (the "License"). You can obtain a copy of the License at
# http://mozilla.org/MPL/2.0/.

import ConfigParser
import datetime
import json
import logging
#import md5
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib2
from bs4 import BeautifulSoup

from logging.handlers import TimedRotatingFileHandler
from emailhandler import SMTPHandler
from daemonize import Daemon

class Job(object):
    def __init__(self, jobmonitor, jobid, email, build, label, jobtype, jobdata,
                 datazilla, status, started, timestamp):
        self.jm = jobmonitor
        self.id = jobid
        self.email = email
        self.build = build
        self.label = label
        self.jobtype = jobtype
        self.jobdata = jobdata
        self.datazilla = datazilla
        self.status = status
        self.started = started
        self.timestamp = timestamp

class JobMonitor(Daemon):
    def __init__(self, options, createdb=False):

        super(JobMonitor, self).__init__(options)

        self.database = options.database
        self.job = None

        config = ConfigParser.RawConfigParser()
        config.readfp(open(options.settings))

        self.server = config.get("server", "server")
        self.results_server = config.get("server", "results_server")
        self.time_limit = config.getint("server", "time_limit")
        self.sleep_time = config.getint("server", "sleep_time")
        self.check_minutes = config.getint("server", "check_minutes")
        try:
            self.port = config.getint("server", "port")
        except ConfigParser.Error:
            self.port = 8051

        #TODO: specific to power
        self.powerconfig = config.get("power_server", "powerconfig")

        self.build_name = None
        self.build_version = None
        self.build_id = None
        self.build_branch = None
        self.build_revision = None

        # TODO: specific to wpt, how to decide which defaults we want
        self.firefoxpath = config.get("wpt_server", "firefoxpath")
        self.api_key = config.get("wpt_server", "api_key")
        self.firefoxdatpath = config.get("wpt_server", "firefoxdatpath")

        self.defaults = {}
        self.defaults['wpt'] = {}
        self.defaults['wpt']['default_locations'] = config.get("wpt_defaults", "locations").split(",")
        self.defaults['wpt']['default_urls'] = config.get("wpt_defaults", "urls").split(",")

        self.admin_toaddrs = config.get("admin", "admin_toaddrs").split(",")
        self.admin_subject = config.get("admin", "admin_subject")
        self.mail_username = config.get("mail", "username")
        self.mail_password = config.get("mail", "password")
        self.mail_host = config.get("mail", "mailhost")

        self.oauth_key = config.get("datazilla", "oauth_consumer_key")
        self.oauth_secret = config.get("datazilla", "oauth_consumer_secret")

        self.admin_loglevel = logging.DEBUG
        try:
            self.admin_loglevel = getattr(logging,
                                          config.get("admin",
                                                     "admin_loglevel"))
        except AttributeError:
            pass
        except ConfigParser.Error:
            pass

        # Set up the root logger to log to a daily rotated file log.
        self.logfile = options.log
        self.logger = logging.getLogger("wpt")
        self.logger.setLevel(self.admin_loglevel)
        filehandler = TimedRotatingFileHandler(self.logfile,
                                               when="D",
                                               interval=1,
                                               backupCount=7,
                                               encoding=None,
                                               delay=False,
                                               utc=False)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        filehandler.setFormatter(formatter)
        self.logger.addHandler(filehandler)

        # Set up the administrative logger with an SMTP handler. It
        # should also bubble up to the root logger so we only need to
        # use it for ERROR or CRITICAL messages.

        self.emaillogger = logging.getLogger("wpt.email")
        self.emaillogger.setLevel(logging.ERROR)
        self.emailhandler = SMTPHandler(self.mail_host,
                                        self.mail_username,
                                        self.admin_toaddrs,
                                        self.admin_subject,
                                        credentials=(self.mail_username,
                                                     self.mail_password),
                                        secure=())
#TODO: JMAHER: turn these emailers back on
#        self.emaillogger.addHandler(self.emailhandler)

        self.userlogger = logging.getLogger("user")
        self.userlogger.propagate = False
        self.userlogger.setLevel(logging.INFO)
        self.userhandler = SMTPHandler(self.mail_host,
                                       self.mail_username,
                                       self.admin_toaddrs,
                                       "user subject",
                                       credentials=(self.mail_username,
                                                    self.mail_password),
                                   secure=())
#        self.userlogger.addHandler(self.userhandler)

        self.automatic_jobs = []
        job_names = []
        try:
            job_names = config.get("automatic", "jobs").split(",")
        except ConfigParser.Error:
            pass
        for job_name in job_names:
            jobtype = config.get(job_name, "type")
            # TODO: JMAHER: make this clenaer
            if jobtype != "power":
                continue

            automatic_job = {}
            self.automatic_jobs.append(automatic_job)
            automatic_job["email"] = config.get(job_name, "email")
            automatic_job["label"] = config.get(job_name, "label")
            automatic_job["build"] = config.get(job_name, "build")
            automatic_job["datazilla"] = config.get(job_name, "datazilla")
            automatic_job["hour"] = config.getint(job_name, "hour")

            # this is power specific
            automatic_job["jobtype"] = jobtype

            # TODO: how to make this a json object instead of a string
            automatic_job["jobdata"] = "{}"

            """
            # TODO: JMAHER: specifc to WPT, we need to make config.get use jobtype specifics
            automatic_job["jobtype"] = "web-page-test"
            automatic_job["jobdata"] = {"urls": config.get(job_name, "urls").split(","),
                                        "locations": config.get(job_name, "locations").split(","),
                                        "speeds": config.get(job_name, "speeds").split(","),
                                        "script": config.get(job_name, "script"),
                                        "runs": config.get(job_name, "runs"),
                                        "tcpdump": config.get(job_name, "tcpdump"),
                                        "video": config.get(job_name, "video")
                                       }
            """
 
           # If the current hour before the scheduled hour for
            # the job, force its submission today. Otherwise, wait until
            # tomorrow to submit the job.
            automatic_job["datetime"] = datetime.datetime.now()
            if automatic_job["datetime"].hour <= automatic_job["hour"]:
                automatic_job["datetime"] -= datetime.timedelta(days=1)

        if os.path.exists(self.database):
            try:
                self.connection = sqlite3.connect(self.database)
                self.connection.execute("PRAGMA foreign_keys = ON;")
                self.cursor = self.connection.cursor()
            except sqlite3.OperationalError:
                self.notify_admin_logger("Failed to start").exception(
                    "Could not get database connection " +
                    "to %s" % self.database)
                exit(2)
        elif not createdb:
                self.notify_admin_logger("Failed to start").error(
                    "database file %s does not exist" %
                    self.database)
                exit(2)
        else:
            try:
                self.connection = sqlite3.connect(options.database)
                self.connection.execute("PRAGMA foreign_keys = ON;")
                self.cursor = self.connection.cursor()
                self.cursor.execute("create table jobs ("
                                    "id integer primary key autoincrement, "
                                    "email text, "
                                    "build text, "
                                    "label text, "
                                    "jobtype text, "
                                    "jobdata text, "
                                    "datazilla text, "
                                    "status text, " 
                                    "started text, "
                                    "timestamp text"
                                    ")"
                                    )
                self.connection.commit()
            except sqlite3.OperationalError:
                self.notify_admin_logger("Failed to start").exception(
                    "SQLError creating schema in " +
                    "database %s" % options.database)
                exit(2)

    def set_job(self, jobid, email, build, label, jobtype, jobdata, 
                datazilla, status, started, timestamp):
        try:
            self.job = Job(self, jobid, email, build, label, jobtype, jobdata,
                           datazilla, status, started, timestamp)
        except:
            self.notify_admin_exception("Error setting job")
            self.notify_user_exception(self.job.email,
                                       "Error setting job")
            self.purge_job(jobid)

    def create_job(self, email, build, label, jobtype, jobdata, datazilla):
        self.set_job(None, email, build, label, jobtype, jobdata, datazilla,
                     None, None, None)
        try:
            self.cursor.execute(
                "insert into jobs(email, build, label, jobtype, jobdata, "
                "datazilla, status, started) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (email, build, label, jobtype, jobdata, datazilla,
                 "waiting",
                 datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
            self.connection.commit()
            self.job.id = jobid = self.cursor.lastrowid
        except sqlite3.OperationalError:
            self.notify_admin_exception("Error inserting job")
            self.notify_user_exception(email, "Error inserting job")
            raise

        self.notify_user_info(email, "job submitted")

    #TODO: JMAHER: can we use a subclass for this?
    def job_email_boilerplate(self, subject, message=None):
        if not message:
            message = ""
        if not self.job:
            job_message = ""
        else:
#TODO: JMAHER: do we need jobtype here?  can we make jobdata better?
            job_message = """
Job:       %(id)s
Label:     %(label)s
Build:     %(build)s
JobType:   %(jobtype)s
JobData:   %(jobdata)s
datazilla: %(datazilla)s
Status:    %(status)s
""" % self.job.__dict__
        job_message = "%s\n\n%s\n\n%s\n\n" % (subject, job_message, message)
        return job_message

    #TODO: JMAHER: we have hard coded subjects!
    def notify_user_logger(self, user, subject):
        """Set the userlogger's handler to address and subject fields
        and return a reference to the userlogger object."""
        if self.job:
            subject = "[WebPagetest] Job %s Label %s %s" % (self.job.id,
                                                            self.job.label,
                                                            subject)
        else:
            subject = "[WebPagetest] %s" % subject
        self.userhandler.toaddrs = [user]
        self.userhandler.subject = subject
        return self.userlogger

    def notify_admin_logger(self, subject):
        """Set the emaillogger's handler subject field
        and return a reference to the emaillogger object."""
        if self.job:
            subject = "[WebPagetest] Job %s Label %s %s" % (self.job.id,
                                                            self.job.label,
                                                            subject)
        else:
            subject = "[WebPagetest] %s" % subject
        self.emailhandler.subject = subject
        return self.emaillogger

    def notify_user_info(self, user, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        self.notify_user_logger(user, subject).info(job_message)

    def notify_user_exception(self, user, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        contact_message = ("Please contact your administrators %s for help." %
                           self.admin_toaddrs)
        job_message = "%s%s" % (job_message, contact_message)
        self.notify_user_logger(user, subject).exception(job_message)

    def notify_user_error(self, user, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        contact_message = ("Please contact your administrators %s for help." %
                           self.admin_toaddrs)
        job_message = "%s%s" % (job_message, contact_message)
        self.notify_user_logger(user, subject).error(job_message)

    def notify_admin_info(self, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        self.notify_admin_logger(subject).info(job_message)

    def notify_admin_exception(self, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        self.notify_admin_logger(subject).exception(job_message)

    def notify_admin_error(self, subject, message=None):
        job_message = self.job_email_boilerplate(subject, message)
        self.notify_admin_logger(subject).error(job_message)

    def purge_job(self, jobid):
        """Purge the job whose id is jobid along with all of the
        linked locations, speeds, and urls.
        """
        if not jobid:
            return

        jobparm = {"jobid": jobid}
        try:
            self.cursor.execute("delete from jobs where id=:jobid",
                                jobparm)
            self.connection.commit()
        except sqlite3.OperationalError:
            self.notify_admin_exception("Exception purging job %s" % jobid)
        finally:
            if self.job and self.job.id == jobid:
                self.job = None

    def check_build(self, build):
        """Check the build url to see if build is available. build can
        be either a direct link to a build or a link to a directory
        containing the build. If the build is available, then
        check_build will return the actual url to the build.
        """
        # TODO(bc) if build is a directory, then we need to pick the
        # latest url.
        buildurl = None
        re_builds = re.compile(r"firefox-([0-9]+).*\.win32\.installer\.exe")

        # TODO: JMAHER: find a way to get the proxy automatically (env, settings.ini, cli, etc.)
        proxy = urllib2.ProxyHandler({'http': 'proxy.dmz.scl3.mozilla.com:3128'})
        opener = urllib2.build_opener(proxy)
        urllib2.install_opener(opener)

        if not build.endswith("/"):
            # direct url to a build implies the build is available now.
            buildurl = build
        else:
            try:
                builddir_content = urllib2.urlopen(build).read()
                builddir_soup = BeautifulSoup(builddir_content)
                for build_link in builddir_soup.findAll("a"):
                    match = re_builds.match(build_link.get("href"))
                    if match:
                        buildurl = "%s%s" % (build, build_link.get("href"))
            except:
                # Which exceptions here? from urllib2, BeautifulSoup
                self.notify_admin_exception("Error checking build")
                buildurl = None

        if buildurl:
            try:
                builddir_content = urllib2.urlopen(build).read()
            except:
                buildurl = None

        return buildurl

    def process_job(self):
        """Get the oldest pending job and start it up.
        """
        try:
            self.cursor.execute(
                "select * from jobs where status = 'pending' order by started")
            jobrow = self.cursor.fetchone()
        except sqlite3.OperationalError:
            self.notify_admin_exception("Error finding pending jobs")
            raise

        if not jobrow:
            return

        (jobid, email, build, label, jobtype, jobdata, datazilla,
         status, started, timestamp) = jobrow
        self.set_job(jobid, email, build, label, jobtype, jobdata, datazilla,
                     status, started, timestamp)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.job.status = status = "running"
        self.logger.debug("jobid: %s, email: %s, build: %s, label: %s, "
                          "jobtype: %s, jobdata: %s, datazilla: %s, "
                          "status: %s, started: %s, timestamp: %s" %
                          (jobid, email, build, label,
                           jobtype, jobdata, datazilla, status,
                           started, timestamp))
        try:
            self.cursor.execute(
                "update jobs set build=:build, status=:status, "
                "timestamp=:timestamp where id=:jobid",
                {"jobid": jobid, "build": build, "status": status,
                 "timestamp": timestamp})
            self.connection.commit()
            self.notify_user_info(email, "job is running")
        except sqlite3.OperationalError:
            self.notify_admin_exception("Error updating running job")
            self.notify_user_exception(email, "Error updating running job")
            self.purge_job(jobid)
            return

        if not self.download_build():
            print "not download_build, huh?"
            self.purge_job(jobid)
            return

        #TODO: JMAHER: this used to be process_location, is this right?
        self.process_details()

        self.job.status = "completed"
        self.notify_user_info(email, "job completed.")
        self.purge_job(jobid)

    def download_build(self):
        """Download a build to the webpagetest server and
        update the firefox.dat file.
        """
        self.logger.debug("downloading build: %s" % self.job.build)
        try:
            if os.path.exists(self.firefoxpath):
                os.unlink(self.firefoxpath)

            proxy = urllib2.ProxyHandler({'http': 'proxy.dmz.scl3.mozilla.com:3128'})
            opener = urllib2.build_opener(proxy)
            urllib2.install_opener(opener)
            builddir_content = urllib2.urlopen(self.job.build).read()
            with open(self.firefoxpath, 'wb') as f:
                f.write(builddir_content)

        except IOError:
            self.notify_admin_exception("Error downloading build")
            self.notify_user_exception(self.job.email, "Error downloading build")
            return False

        if not self.edit_config_file():
            print "error editing config file"
            return False

        # Get information about the build by extracting the installer
        # to a temporary directory and parsing the application.ini file.
        tempdirectory = tempfile.mkdtemp()
        returncode = subprocess.call(["7z", "x", self.firefoxpath,
                                      "-o%s" % tempdirectory])
        appini = ConfigParser.RawConfigParser()
        appini.readfp(open("%s/core/application.ini" % tempdirectory))
        self.build_name = appini.get("App", "name")
        self.build_version = appini.get("App", "version")
        self.build_id = appini.get("App", "buildID")
        self.build_branch = os.path.basename(appini.get("App", "SourceRepository"))
        self.build_revision = appini.get("App", "SourceStamp")

        self.logger.debug("build_name: %s" % self.build_name)
        self.logger.debug("build_version: %s" % self.build_version)
        self.logger.debug("build_id: %s" % self.build_id)
        self.logger.debug("build_branch: %s" % self.build_branch)
        self.logger.debug("build_revision: %s" % self.build_revision)

        if returncode != 0:
            raise Exception("download_build: "
                            "error extracting build: rc=%d" % returncode)
        shutil.rmtree(tempdirectory)

        # delay after updating firefox.dat to give the clients time to
        # check for the updated build.
        time.sleep(120)
        return True

    #TODO: JMAHER: figure out test_msg_map + messages
    def process_test_results(self, jobtype, jobdata, test_msg_map, messages):
        #TODO: JMAHER: this should be a stub
        pass

    def post_to_datazilla(self, test_results):
        #TODO: JMAHER: we should subclass this stuff
        pass

    def check_waiting_jobs(self):
        """Check waiting jobs that are older than check_minutes or
        that have not been checked yet to see if builds are
        available. If they are available, switch them to pending.
        """
        check_threshold = ((datetime.datetime.now() -
                           datetime.timedelta(minutes=self.check_minutes)).
                           strftime("%Y-%m-%dT%H:%M:%S"))
        try:
            self.cursor.execute(
                "select * from jobs where status = 'waiting' and "
                "(timestamp is NULL or timestamp < :check_threshold)",
                {"check_threshold": check_threshold})
            jobrows = self.cursor.fetchall()
        except sqlite3.OperationalError:
            self.notify_admin_exception("Error checking waiting jobs")
            raise

        for jobrow in jobrows:
            (jobid, email, build, label, jobtype, jobdata, datazilla,
             status, started, timestamp) = jobrow
            self.set_job(jobid, email, build, label, jobtype, jobdata,
                         datazilla, status, started, timestamp)

            self.logger.debug("checking_waiting_jobs: "
                              "jobid: %s, email: %s, build: %s, label: %s, "
                              "datazilla: %s, "
                              "status: %s, started: %s, timestamp: %s" %
                              (jobid, email, build, label,
                               datazilla, status,
                               started, timestamp))
            try:
                buildurl = self.check_build(build)
            except:
                self.notify_admin_exception("Build Error")
                self.notify_user_exception(email, "Build Error")
                self.purge_job(jobid)
                continue

            if buildurl:
                self.job.status = status = "pending"
                build = buildurl
            try:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                self.cursor.execute("update jobs set build=:build, "
                               "status=:status, timestamp=:timestamp "
                               "where id=:jobid",
                               {"jobid": jobid, "build": build,
                                "status": status, "timestamp": timestamp})
                self.connection.commit()
                self.notify_user_info(email,
                                      "job is pending availability of the build.")
            except sqlite3.OperationalError:
                self.notify_admin_exception("Error updating job")
                self.notify_user_exception(email, "job failed")
                self.purge_job(jobid)

    def check_running_jobs(self):
        """Check the running job if any.
        """
        try:
            self.cursor.execute("select * from jobs where status = 'running'")
            jobrows = self.cursor.fetchall()
        except sqlite3.OperationalError:
            self.notify_admin_exception("Error checking running jobs")
            raise

        if jobrows:
            ### We should never get here unless we crashed while processing
            ### a job. Lets just delete any jobs with 'running' status and
            ### notify the user.
            for jobrow in jobrows:
                # send email to user then delete job
                (jobid, email, build, label, jobtype, jobdata, datazilla,
                 status, started, timestamp) = jobrow
                self.set_job(jobid, email, build, label, jobtype, jobdata,
                             datazilla, status, started, timestamp)
                self.purge_job(jobid)

    def check_automatic_jobs(self):
        """If the current datetime is a calendar day later than the last
        time the automatic job was submitted and if the current hour
        is later than the automatic job's scheduled hour, submit the job."""
        now = datetime.datetime.now()
        for aj in self.automatic_jobs:
            aj_datetime = aj["datetime"]
            aj_hour = aj["hour"]
            if (now > aj_datetime and now.day != aj_datetime.day and
                now.hour >= aj_hour):
                self.create_job(aj["email"],
                                aj["build"],
                                aj["label"],
                                aj["jobtype"],
                                aj["jobdata"],
                                aj["datazilla"]);
                aj["datetime"] = now


