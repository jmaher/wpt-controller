from monitor import JobMonitor
from optparse import OptionParser
import os
import time
import json
import subprocess

class PowerMonitor(JobMonitor):

    def edit_config_file(self):
        try:
            with open(self.powerconfig, 'r') as f:
                pconfig = json.load(f)

            with open(self.powerconfig, 'w') as f:
                iter = 0
                for product in pconfig["OS"]["Windows"]:
                    if product["name"] == "Firefox":
                        pconfig["OS"]["Windows"][iter]["url"] = self.job.build
                    iter = iter + 1
                json.dump(pconfig, f)
                return True
        except IOError:
            self.notify_admin_exception("Error writing file: %s" % self.powerconfig)
            self.notify_user_exception(self.job.email, "job failed")
            return False


    def process_details(self):
        """Submit jobs for this location for each speed and url.
        """
        #TODO: we need to queue these up somehow- launch one at a time and make this serial
        #TODO: JMAHER: we need to get cwd in from a config file
        p = subprocess.Popen(['python3', 'benchmark.py', '--is_dispatcher'], cwd='/home/mozauto/power_logger', stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print p.communicate()[0]

    def process_test_results(self, location, test_speed_map, test_url_map,
                             test_msg_map, messages):
        """Process test results, notifying user of the results.
        """
        build_name = ""
        build_version = ""
        build_revision = ""
        build_id = ""
        build_branch = ""


        if build_name:
            msg_body += "%s %s %s id: %s revision: %s\n\n" % (build_name,
                                                              build_version,
                                                              build_branch,
                                                              build_id,
                                                              build_revision)

        msg_body_keys = msg_body_map.keys()
        msg_body_keys.sort()
        if len(msg_body_keys) == 0:
            messages += "No results were found."
        else:
            for msg_body_key in msg_body_keys:
                msg_body += msg_body_map[msg_body_key]
        if messages:
            msg_body += "\n\n%s\n" % messages
        self.notify_user_info(self.job.email, msg_subject, msg_body)

    #TODO: JMAHER: extract this out as data model for wpt is specific
    def post_to_datazilla(self, test_result):
        """ take test_results (json) and upload them to datazilla """

        # We will attach wpt_data to the datazilla result as a top
        # level attribute to store out of band data about the test.
        submit_results = False
        if self.job.datazilla == "on":
            # Do not short circuit the function but collect
            # additional data for use in emailing the user
            # before returning.
            submit_results = True

        result = DatazillaResult()
        result.add_testsuite(suite_name)
        #TODO: JMAHER: hardcoded microperf here, this project should be in a config file and a real name
        request = DatazillaRequest("https",
                                   "datazilla.mozilla.org",
                                   "microperf",
                                   self.oauth_key,
                                   self.oauth_secret,
                                   machine_name=machine_name,
                                   os=os_name,
                                   os_version=os_version,
                                   platform=platform,
                                   build_name=self.build_name,
                                   version=self.build_version,
                                   revision=self.build_revision,
                                   branch=self.build_branch,
                                   id=self.build_id)
        request.add_datazilla_result(result)
        datasets = request.datasets()
        for dataset in datasets:
            dataset["wpt_data"] = wpt_data
            if not submit_results:
                continue
            response = request.send(dataset)
            # print error responses
            if response.status != 200:
                # use lower-case string because buildbot is sensitive to upper case error
                # as in 'INTERNAL SERVER ERROR'
                # https://bugzilla.mozilla.org/show_bug.cgi?id=799576
                reason = response.reason.lower()
            else:
                res = response.read()
        return datasets

#TODO: JMAHER: figure out a cleaner way to do this for wpt and power
def main():

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
                      default="wptmonitor.log",
                      help="Path to log file. "
                      "Defaults to wptmonitor.log in current directory.")

    parser.add_option("--settings",
                      action="store",
                      type="string",
                      dest="settings",
                      default="settings.ini",
                      help="Path to configuration file. "
                      "Defauls to settings.ini in current directory.")

    parser.add_option("--pidfile",
                      action="store",
                      type="string",
                      default="/var/run/wptmonitor.pid",
                      help="File containing process id of wptcontroller "
                      "if --daemonize is specified.")

    parser.add_option("--daemonize",
                      action="store_true",
                      default=False,
                      help="Runs wptmonitor in daemon mode.")

    (options, args) = parser.parse_args()

    if not os.path.exists(options.settings):
        print "Settings file %s does not exist" % options.settings
        exit(2)

    jm = PowerMonitor(options)

    try:
        while True:
            jm.check_automatic_jobs()
            jm.check_waiting_jobs()
            jm.check_running_jobs()
            jm.process_job()
            time.sleep(jm.sleep_time)
    except:
        jm.notify_admin_exception("Error in wptmonitor",
                                  "Terminating wptmonitor due to " +
                                  "unhandled exception: ")
        exit(2)

if __name__ == "__main__":
    main()

