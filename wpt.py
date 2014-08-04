from monitor import JobMonitor
import random

class WPTMonitor(JobMonitor):

    def edit_config_file(self):
        try:
            builddat = open(self.firefoxdatpath, "w")
            builddat.write("browser=Firefox\n")
            builddat.write("url=http://%s/installers/browsers/"
                           "firefox-installer.exe\n" % self.server)
            #builddat.write("md5=%s\n" % md5digest)
            # need to create a random version here so wpt will install it.
            builddat.write("version=%d\n" % int(100*random.random()))
            builddat.write("command=firefox-installer.exe "
                           "/INI=c:\\webpagetest\\firefox.ini\n")
            builddat.write("update=1\n")
            builddat.close()
        except IOError:
            self.notify_admin_exception("Error writing file: %s" % self.firefoxdatpath)
            self.notify_user_exception(self.job.email, "job failed")
            return False

    def create_job(self, email, build, label, jobtype, jobdata, datazilla):
        self.job.locations = jobdata["locations"]
        self.job.speeds = jobdata["speeds"]
        self.job.urls = jobdata["urls"]
        JobMonitor.create_job(self, email, build, label, jobtype, jobdata, datazilla)

    def process_details(self, location):
        """Submit jobs for this location for each speed and url.
        """
        self.logger.debug("process_details: %s" % location)

        # We can submit any number of speeds and urls for a given
        # location, but we can't submit more than one location at
        # a time since it might affect the network performance if
        # multiple machines are downloading builds, running tests
        # simultaneously.

        def add_msg(test_msg_map, test_id, msg):
            if test_id not in test_msg_map:
                test_msg_map[test_id] = ""
            else:
                test_msg_map[test_id] += ", "
            test_msg_map[test_id] += msg

        messages = ""
        test_url_map = {}
        test_speed_map = {}
        for speed in self.job.speeds:
            self.logger.debug("process_details: location: %s, speed: %s" %
                              (location, speed))

            # The location parameter submitted to wpt's
            # runtest.php is of the form:
            # location:browser.connectivity

            wpt_parameters = {
                "f": "json",
                "private": 0,
                "priority": 6,
                "video": 1,
                "fvonly": 0,
                "label": self.job.label,
                "runs": self.job.runs,
                "tcpdump": self.job.tcpdump,
                "video": self.job.video,
                "location": "%s.%s" % (location, speed),
                "mv": 0,
                "script": self.job.script,
                "k": self.api_key,
            }

            self.logger.debug(
                "submitting batch: email: %s, build: %s, "
                "label: %s, location: %s, speed: %s, urls: %s, "
                "wpt_parameters: %s, server: %s"  % (
                    self.job.email, self.job.build,
                    self.job.label, location, speed, self.job.urls,
                    wpt_parameters, self.server))
            partial_test_url_map = {}
            for url in self.job.urls:
                if self.job.script:
                    wpt_parameters['script'] = '%s\nnavigate\t%s\n' % (self.job.script, url)

                else:
                    wpt_parameters['url'] = url
                request_url = 'http://%s/runtest.php?%s' % (self.server,
                                                            urllib.urlencode(wpt_parameters))
                response = urllib.urlopen(request_url)
                if response.getcode() == 200:
                    response_data = json.loads(response.read())
                    if response_data['statusCode'] == 200:
                        partial_test_url_map[response_data['data']['testId']] = url
            self.logger.debug("partial_test_url_map: %s" % partial_test_url_map)
            accepted_urls = partial_test_url_map.values()
            for url in self.job.urls:
                if url not in accepted_urls:
                    messages += "url %s was not accepted\n" % url
            test_url_map.update(partial_test_url_map)
            for test_id in partial_test_url_map.keys():
                test_speed_map[test_id] = speed

        test_msg_map = {}
        pending_test_url_map = dict(test_url_map)

        # terminate the job after each url has been sufficient time to:
        # load each url 3 times (once to prime wpr, once for first load,
        # once for second load) times the number of runs times the time
        # limit for a test.
        total_time_limit = (len(accepted_urls) * 3 * int(self.job.runs) *
                            self.time_limit)
        terminate_time = (datetime.datetime.now() +
                          datetime.timedelta(seconds=total_time_limit))

        while pending_test_url_map:
            self.logger.debug("pending_test_url_map: %s" % pending_test_url_map)
            if datetime.datetime.now() > terminate_time:
                test_ids = [test_id for test_id in pending_test_url_map]
                for test_id in test_ids:
                    del pending_test_url_map[test_id]
                    add_msg(test_msg_map, test_id,
                            "abandoned due to time limit.")
                continue
            self.logger.debug(
                "CheckBatchStatus: email: %s, build: %s, label: %s, "
                "location: %s, speed: %s, urls: %s" % (
                    self.job.email, self.job.build, self.job.label,
                    location, speed, self.job.urls))
            test_status_map = {}
            for test_id in pending_test_url_map.keys():
                request_url = 'http://%s/testStatus.php?f=json&test=%s' % (self.server,
                                                                           test_id)
                response = urllib.urlopen(request_url)
                if response.getcode() == 200:
                    response_data = json.loads(response.read())
                    test_status = response_data['statusCode']
                    test_status_map[test_id] = test_status
                    if test_status == 100:
                        test_status_text = "started"
                    elif test_status == 101:
                        test_status_text = "waiting"
                    elif test_status == 200:
                        test_status_text = "complete"
                        del pending_test_url_map[test_id]
                    elif test_status == 400 or test_status == 401:
                        test_status_text = "not found"
                        del pending_test_url_map[test_id]
                        add_msg(test_msg_map, test_id, "not found")
                    elif test_status == 402:
                        test_status_text = "cancelled"
                        del pending_test_url_map[test_id]
                        add_msg(test_msg_map, test_id, "cancelled")
                    else:
                        test_status_text = "unexpected failure"
                        del pending_test_url_map[test_id]
                        add_msg(test_msg_map, test_id,
                                "failed with unexpected status %s" % test_status)
                    self.logger.debug("processing test status %s %s %s" %
                                      (test_id, test_status, test_status_text))

            if pending_test_url_map:
                self.logger.debug("Finished checking batch status, "
                                  "sleeping %d seconds..." % self.sleep_time)
                time.sleep(self.sleep_time)

        if messages:
            messages = "\n" + messages

        #TODO: JMAHER: convert this to jobdata
        self.process_test_results(location, test_speed_map, test_url_map,
                                  test_msg_map, messages)

    def process_test_results(self, location, test_speed_map, test_url_map,
                             test_msg_map, messages):
        """Process test results, notifying user of the results.
        """
        build_name = ""
        build_version = ""
        build_revision = ""
        build_id = ""
        build_branch = ""

        msg_subject = "Results for location %s." % location
        msg_body = "Results for location %s\n\n" %  location
        msg_body_map = {}
        for test_id in test_url_map.keys():
            url = test_url_map[test_id]
            speed = test_speed_map[test_id]
            msg_body_key = url + speed

            try:
                msg = "Messages: %s\n\n" % test_msg_map[test_id]
            except KeyError:
                msg = ""
            msg_body_map[msg_body_key] = "\n".join([
                "Url: %s" % url,
                "Speed: %s" % speed,
                "Result: http://%s/result/%s/\n" % (self.results_server, test_id),
                "%s" % msg])
            result_url = "http://%s/jsonResult.php?test=%s" % (self.server,
                                                               test_id)
            self.logger.debug("Getting result for test %s result_url %s" %
                              (test_id, result_url))
            result_response = urllib.urlopen(result_url)
            if result_response.getcode() != 200:
                msg = "Failed to retrieve results from Webpagetest"
                msg_body_map[msg_body_key] += msg
                self.notify_admin_error(msg)
            else:
                test_result = json.loads(result_response.read())
                if test_result["statusCode"] == 200:
                    try:
                        datazilla_dataset = self.post_to_datazilla(test_result)[0]
                        if not build_version:
                            test_build_data = datazilla_dataset["test_build"]
                            build_name = test_build_data["name"]
                            build_version = test_build_data["version"]
                            build_revision = test_build_data["revision"]
                            build_id = test_build_data["id"]
                            build_branch = test_build_data["branch"]
                        wpt_data = datazilla_dataset["wpt_data"]
                        for view in "firstView", "repeatView":
                            view_data = wpt_data[view]
                            msg_body_map[msg_body_key] += "  %s:\n" % view
                            view_data_keys = view_data.keys()
                            view_data_keys.sort()
                            for data_key in view_data_keys:
                                msg_body_map[msg_body_key] += "    %s: %s\n" % (data_key, view_data[data_key])
                        msg_body_map[msg_body_key] += "\n"
                    except:
                        msg = "Error processing test result into datazilla"
                        msg_body_map[msg_body_key] += msg
                        self.notify_admin_exception(msg)
                if self.admin_loglevel == logging.DEBUG:
                    import os.path
                    logdir = os.path.dirname(self.logfile)
                    result_txt = open(os.path.join(logdir, "results-%s.txt" % test_id), "a+")
                    result_txt.write(msg_body)
                    result_txt.close()
                    result_json = open(os.path.join(logdir, "results-%s.json" % test_id), "a+")
                    result_json.write(json.dumps(test_result, indent=4, sort_keys=True) + "\n")
                    result_json.close()
                test_result = None

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
        wpt_data = {
            "url": "",
            "firstView": {},
            "repeatView": {}
        }
        wpt_data["label"] = test_result["data"]["label"]
        submit_results = False
        if self.job.datazilla == "on":
            # Do not short circuit the function but collect
            # additional data for use in emailing the user
            # before returning.
            submit_results = True

        self.logger.debug('Submit results to datazilla: %s' % self.job.datazilla)
        wpt_data["connectivity"] = test_result["data"]["connectivity"]
        wpt_data["location"] = test_result["data"]["location"]
        wpt_data["url"] = test_result["data"]["url"]
        runs = test_result["data"]["runs"]

        # runs[0] is a dummy entry
        # runs[1]["firstView"]["SpeedIndex"]
        # runs[1]["repeatView"]["SpeedIndex"]
        # runs[1]["firstView"]["requests"][0]["headers"]["request"][2]
        #    "User-Agent: Mozilla/5.0 (Windows NT 5.1; rv:26.0) Gecko/20100101 Firefox/26.0 PTST/125"

        wpt_metric_keys = ['TTFB', 'render', 'docTime', 'fullyLoaded',
                           'SpeedIndex', 'SpeedIndexDT', 'bytesInDoc',
                           'requestsDoc', 'domContentLoadedEventStart',
                           'visualComplete']
        for wpt_key in wpt_metric_keys:
            for view in "firstView", "repeatView":
                wpt_data[view][wpt_key] = []

        if len(runs) == 1:
            raise Exception("post_to_datazilla: no runs")
        os_version = "unknown"
        os_name = "unknown"
        platform = "x86"
        reUserAgent = re.compile('User-Agent: Mozilla/5.0 \(Windows NT ([^;]*);.*')
        for run in runs:
            for wpt_key in wpt_metric_keys:
                for view in "firstView", "repeatView":
                    if not run[view]:
                        continue
                    if wpt_key in run[view]:
                        if run[view][wpt_key]:
                            wpt_data[view][wpt_key].append(run[view][wpt_key])
                    if os_name == "unknown":
                        try:
                            requests = run[view]["requests"]
                            if requests and len(requests) > 0:
                                request = requests[0]
                                if request:
                                    headers = request["headers"]
                                    if headers:
                                        request_headers = headers["request"]
                                        if request_headers:
                                            for request_header in request_headers:
                                                if "User-Agent" in request_header:
                                                    match = re.match(reUserAgent,
                                                                     request_header)
                                                    if match:
                                                        os_name = "WINNT"
                                                        os_version = match.group(1)
                                                        break
                        except KeyError:
                            pass

        machine_name = wpt_data["location"].split(":")[0]
        # limit suite name to 128 characters to match mysql column size
        suite_name = (wpt_data["location"] + "." + wpt_data["connectivity"])[:128]
        # limit {first,repeat}_name, to 255 characters to match mysql column size
        # leave protocol in the url in order to distinguish http vs https.
        first_name = wpt_data["url"][:252] + ":fv"
        repeat_name = wpt_data["url"][:252] + ":rv"

        result = DatazillaResult()
        result.add_testsuite(suite_name)
        result.add_test_results(suite_name, first_name, wpt_data["firstView"]["SpeedIndex"])
        result.add_test_results(suite_name, repeat_name, wpt_data["repeatView"]["SpeedIndex"])
        request = DatazillaRequest("https",
                                   "datazilla.mozilla.org",
                                   "webpagetest",
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
                self.logger.debug("Error posting to %s %s %s: %s %s" % (
                    wpt_data["url"], wpt_data["location"], wpt_data["connectivity"],
                    response.status, reason))
            else:
                res = response.read()
                self.logger.debug("Datazilla response for %s %s %s is: %s" % (
                    wpt_data["url"], wpt_data["location"], wpt_data["connectivity"],
                    res.lower()))
        return datasets

