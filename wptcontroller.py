#!/usr/bin/env python
# This Source Code is subject to the terms of the Mozilla Public License
# version 2.0 (the "License"). You can obtain a copy of the License at
# http://mozilla.org/MPL/2.0/.

# modified from http://webpython.codepoint.net/wsgi_request_parsing_post
# to save submitted jobs.

# from autophone's jobs.py
import ConfigParser
import logging
import sqlite3
import os
import json
import datetime

from wsgiref.simple_server import make_server
from cgi import parse_qs, escape

from logging.handlers import TimedRotatingFileHandler
from emailhandler import SMTPHandler
from daemonize import Daemon
from wptmonitor import JobMonitor

def application(environ, start_response):
    email = ""
    build = ""
    label = ""
    runs = ""
    tcpdump = ""
    video = ""
    locations = []
    speeds = []
    urls = []

    if "REQUEST_METHOD" not in environ:
        status = "501 Not Implemented"
        response_body = "Missing REQUEST_METHOD: %s" % status
        response_headers = [("Content-Type", "text/html"),
                            ("Content-Length", str(len(response_body)))]
        start_response(status, response_headers)
        return [str(response_body)]

    if not environ["REQUEST_METHOD"] in "GET,POST":
        status = "405 Method Not Allowed"
        response_headers = [("Allow", "GET,POST")]
        start_response(status, response_headers)
        return []

    if environ["REQUEST_METHOD"] == "POST":
        # the environment variable CONTENT_LENGTH may be empty or missing
        try:
            request_body_size = int(environ.get("CONTENT_LENGTH", 0))
        except (ValueError):
            request_body_size = 0

        # When the method is POST the query string will be sent
        # in the HTTP request body which is passed by the WSGI server
        # in the file like wsgi.input environment variable.
        request_body = environ["wsgi.input"].read(request_body_size)
        try:
            d = json.loads(request_body)
            email = d.get("email", [""])[0]
            build = d.get("build", [""])[0]
            label = d.get("label", [""])[0]
            runs = d.get("runs", [""])[0]
            tcpdump = d.get("tcpdump", [""])[0]
            video = d.get("video", [""])[0]
        except:
            d = parse_qs(request_body)
            email = d.get("email", [""])[0]
            build = d.get("build", [""])[0]
            label = d.get("label", [""])[0]
            runs = d.get("runs", [""])[0]
            tcpdump = d.get("tcpdump", [""])[0]
            video = d.get("video", [""])[0]

        locations = d.get("locations", [])
        speeds = d.get("speeds", [])
        urls = d.get("urls", [])

        # Always escape user input to avoid script injection
        email = escape(email.strip())
        build = escape(build.strip())
        label = escape(label.strip())
        runs = escape(runs.strip())
        tcpdump = escape(tcpdump.strip())
        video = escape(video.strip())
        locations = [escape(location.strip()) for location in locations]
        speeds = [escape(speed.strip()) for speed in speeds]
        urls = [escape(url.strip()) for url in urls]

        jm.set_job(None, email, build, label, runs, tcpdump, video,
                   None, None, None)
        jm.job.locations = locations
        jm.job.speeds = speeds
        jm.job.urls = urls

        try:
            jm.cursor.execute(
                "insert into jobs(email, build, label, runs, tcpdump, video, "
                "status, started) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                (email, build, label, runs, tcpdump, video, "waiting",
                 datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
            jm.connection.commit()
            jm.job.id = jobid = jm.cursor.lastrowid
        except sqlite3.OperationalError:
            jm.notify_admin_exception("Error inserting job")
            jm.notify_user_exception(email, "Error inserting job")
            raise

        for location in locations:
            try:
                jm.cursor.execute(
                    "insert into locations(location, jobid) "
                    "values (?, ?)",
                    (location, jobid))
                jm.connection.commit()
            except sqlite3.OperationalError:
                jm.notify_admin_exception("Error inserting location")
                jm.notify_user_exception(email, "Error inserting location")
                jm.purge_job(jobid)
                raise

        for speed in speeds:
            try:
                jm.cursor.execute(
                    "insert into speeds(speed, jobid) values (?, ?)",
                    (speed, jobid))
                jm.connection.commit()
            except sqlite3.OperationalError:
                msg = ("SQLError inserting speed: email: %s, build: %s, "
                       "label: %s, location: %s" % (email, build, label, speed))
                jm.notify_admin_exception("Error inserting speed")
                jm.notify_user_exception(email, "Error inserting speed")
                jm.purge_job(jobid)
                raise

        for url in urls:
            try:
                jm.cursor.execute(
                    "insert into urls(url, jobid) values (?, ?)",
                    (url, jobid))
                jm.connection.commit()
            except sqlite3.OperationalError:
                msg = ("SQLError inserting url: email: %s, build: %s, "
                       "label: %s, url: %s" % (email, build, label, url))
                jm.notify_admin_exception("Error inserting url")
                jm.notify_user_exception(email, "Error inserting url")
                jm.purge_job(jobid)
                raise

        jm.notify_user_info(email, "job submitted")
        status = "302 Found"
        response_headers = [("Location", "/wpt-controller")]
        start_response(status, response_headers)
        return []

    currentteststable = ""

    try:
        jm.cursor.execute("select * from jobs")
        jobrows = jm.cursor.fetchall()
    except sqlite3.OperationalError:
        jm.notify_admin_exception("Error displaying current jobs")
        raise

    if jobrows:
        currentteststable = "<table><caption>Current Tests</caption>"

    for jobrow in jobrows:
        (jobid, email, build, label, runs, tcpdump, video, status, started,
         timestamp) = jobrow
        jm.set_job(jobid, email, build, label, runs, tcpdump, video,
                   status, started, timestamp)
        try:
            jm.cursor.execute(
                "select * from locations where jobid=:jobid",
                {"jobid": jobid})
            locationrows = jm.cursor.fetchall()
        except sqlite3.OperationalError:
            jm.notify_admin_exception("Error displaying current jobs",
                                      "SQLError selecting locations for job %s." %
                                      jobid)
            raise

        try:
            jm.cursor.execute(
                "select * from speeds where jobid=:jobid",
                {"jobid": jobid})
            speedrows = jm.cursor.fetchall()
        except sqlite3.OperationalError:
            jm.notify_admin_exception("Error displaying current jobs",
                                      "SQLError selecting speeds for job %s." %
                                      jobid)
            raise

        try:
            jm.cursor.execute(
                "select * from urls where jobid=:jobid",
                {"jobid": jobid})
            urlrows = jm.cursor.fetchall()
        except sqlite3.OperationalError:
            jm.notify_admin_exception("Error displaying current jobs",
                                      "SQLError selecting urls for job %s." %
                                      jobid)
            raise

        currentteststable += (
            "<tr>" +
            "<th>job id</th><th>user email</th><th>build</th>" +
            "<th>label</th><th>runs</th><th>tcpdump</th>" +
            "<th>video</th><th>status</th><th>started</th>" +
            "<th>timestamp</th>" +
            "<th>location</th>" +
            "<th>speed</th>" +
            "<th>url</th>" +
            "</tr>")

        for locationrow in locationrows:
            for speedrow in speedrows:
                for urlrow in urlrows:
                    args = []
                    args.extend(jobrow)
                    args.append(locationrow[1])
                    args.append(speedrow[1])
                    args.append(urlrow[1])

                    currentteststable += (
                        ("<tr>" +
                         "<td>%s</td><td>%s</td><td>%s</td>" +
                         "<td>%s</td><td>%s</td><td>%s</td>" +
                         "<td>%s</td><td>%s</td><td>%s</td>" +
                         "<td>%s</td>" +
                         "<td>%s</td>" +
                         "<td>%s</td>" +
                         "<td>%s</td>" +
                         "</tr>") % tuple(args))

    if jobrows:
        currentteststable += "</table>"

    response_body = html % currentteststable
    status = "200 OK"
    response_headers = [("Content-Type", "text/html"),
                        ("Content-Length", str(len(response_body)))]
    start_response(status, response_headers)
    return [str(response_body)]

if __name__ == "__main__":

    from optparse import OptionParser

    parser = OptionParser()

    parser.add_option("--database",
                      action="store",
                      type="string",
                      dest="database",
                      default="jobmanager.sqlite",
                      help="Path to sqlite3 database file. "
                      "Defaults to jobmanager.sqlite in current directory.")

    parser.add_option("--log",
                      action="store",
                      type="string",
                      dest="log",
                      default="wptcontroller.log",
                      help="Path to log file. "
                      "Defaults to wptcontroller.log in current directory.")

    parser.add_option("--settings",
                      action="store",
                      type="string",
                      dest="settings",
                      default="settings.ini",
                      help="Path to configuration file. "
                      "Defaults to settings.ini in current directory.")

    parser.add_option("--pidfile",
                      action="store",
                      type="string",
                      default="/var/run/wptcontroller.pid",
                      help="File containing process id of wptcontroller "
                      "if --daemonize is specified.")

    parser.add_option("--daemonize",
                      action="store_true",
                      default=False,
                      help="Runs wptcontroller in daemon mode.")

    (options, args) = parser.parse_args()

    jm = JobMonitor(options, createdb=True)

    html = """
<!DOCTYPE html>
<html>
  <title>Mozilla WebPagetest Controller</title>
  <style type="text/css">
    caption, label {font-weight: bold;}
    #wrapper { margin-left: auto; margin-right: auto; width: 50%% }
  </style>
  <body>
    <div id="wrapper">
      <h1>Mozilla WebPagetest Controller</h1>
      <form method="post" action="wpt-controller">
        <p>
          <label>Email: <input type="text" name="email" maxlength="2048"></label>
        </p>
        <p>
          <label>Build: <input type="text" name="build"></label>
        </p>
        <p>
          <label>Label: <input type="text" name="label"></label>
        </p>
        <p>
          <label>Runs: <input type="text" name="runs" value="1"></label>
        </p>
        <p>
          <label>tcpdump: <input type="checkbox" name="tcpdump" checked="checked">
          </label>
        </p>
        <p>
          <label>video: <input type="checkbox" name="video" checked="checked">
          </label>
        </p>
        <p>
          <!--
            These are the predefined connection speeds in webpagetest's
            settings/connectivity.ini. We can add additional speeds there
            by adding new sections and listing the options here.
          -->
          <label for="speeds">Speeds:</label>
        </p>
        <select id="speeds" name="speeds" size="9" multiple>
          <option value="Broadband">Broadband (10 Mbps/10Mbps 90ms RTT)</option>
          <option value="ModernMobile">Modern Mobile (1 Mbps/1Mbps 150ms RTT)</option>
          <option value="ClassicMobile">Classic Mobile (400 Kbps/400 Kbps 300ms RTT)</option>
          <option value="Native">Native Connection (No Traffic Shaping)</option>
          <option value="Fios">FIOS (20/5 Mbps 4ms RTT</option>
          <option value="Cable">Cable (5/1 Mbps 28ms RTT)</option>
          <option value="DSL">DSL (1.5 Mbps/384 Kbps 50ms RTT)</option>
          <option value="3G">3G (1.6 Mbps/768 Kbps 300ms RTT)</option>
          <option value="Dial">56K Dial-Up (49/30 Kbps 120ms RTT)</option>
        </select>
        <p>
          <label for="urls">URLS:</label>
        </p>"""
    html += ("<select id='urls' name='urls' size='" +
             str(len(jm.default_urls)) + "' multiple>" +
             "".join(["<option>" +
                      url +
                      "</option>" for url in jm.default_urls]) +
             "</select>")
    html += """
        <p>
          <label for="locations">Locations:</label>
        </p>
        <select id="location" name="locations" multiple>"""
    html += "".join(["<option>" + location + "</option>" for location in
                       jm.default_locations])
    html += """
        </select>
        <p>
          <input type="submit" value="Submit">
        </p>
        </form>
        %s
    </div>
  </body>
</html>
"""
    try:
        httpd = make_server("localhost", jm.port, application)
        httpd.serve_forever()
    except:
        jm.notify_admin_exception("Error in wsgi server",
                                  "wptcontroller has terminated due to an uncaught exception.")


