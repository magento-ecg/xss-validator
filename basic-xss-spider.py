#!/usr/bin/env python2
# -*- coding: utf-8 -*-

import gevent.monkey
gevent.monkey.patch_all()

import re
import logging
import gevent.pool
import gevent.queue
import urllib
import time
import requests
import mechanize
import cookielib
import gevent
#from BeautifulSoup import UnicodeDammit
import lxml.html
import sys
import argparse
from random import randrange
from urlparse import urlparse
import socket
socket.setdefaulttimeout(30)

from xss_tester import XSS_tester



def parse_args():
    ''' Create arguments '''
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", help="The seed URL to scheduler crawling")
    parser.add_argument("-p", "--parallel", default=500, help="Specifies how many pages you want to crawl in parallel. Default = 500")
    return parser.parse_args()

class Spider:

    def __init__(self, args):

        # Set up the logging
        self.logfile = open('vulnerable_URLs.txt', 'a')
        #logging.basicConfig(level=logging.DEBUG)
        #logging.basicConfig(level=logging.ERROR)
        logging.basicConfig(level=None)
        self.logger = logging.getLogger(__name__)
        self.handler = logging.FileHandler('crawler.log', 'a')
        self.handler.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)

        self.netQ = gevent.queue.Queue()
        self.netPool = gevent.pool.Pool(20)
        self.processingQ = gevent.queue.Queue()
        self.processingPool = gevent.pool.Pool(10)

        self.allLinks = set()
        self.filtered = 0
        #self.br = self.browser()
        self.hit = []
        self.XSS = XSS_tester()
        self.pload = list(self.XSS.payloadTest)

        self.base_url = args.url
        self.seed_url = requests.get(self.base_url, headers = {"User-Agent":self.get_user_agent()}, timeout=30, verify=False).url
        self.base_path = self.url_processor(self.base_url)[3]
        self.allLinks.add(self.seed_url)
        self.netQ.put((self.seed_url, None))

    def browser(self):
        br = mechanize.Browser()

        cj = cookielib.LWPCookieJar()
        br.set_cookiejar(cj)

        br.set_handle_equiv(True)
        br.set_handle_gzip(True)
        br.set_handle_redirect(True)
        br.set_handle_referer(True)
        br.set_handle_robots(False)

# Follows refresh 0 but not hangs on refresh > 0
        br.set_handle_refresh(mechanize._http.HTTPRefreshProcessor(), max_time=1)
        return br

    def start(self):
        self.scheduler_glet = gevent.spawn(self.scheduler)
        self.scheduler_glet.join()

    def scheduler(self):
        try:
            start_time = time.time()
            while 1:

                # Spawn network conns for each unprocessed link
                if not self.netQ.empty():
                    for x in xrange(0, min(self.netQ.qsize(), self.netPool.free_count())):
                        self.netPool.spawn(self.netWorker, None) # set payload to None since we're just checking links
                # Spawn worker to process html
                if not self.processingQ.empty():# and not self.processingPool.full():# and self.processingPool.free_count() != self.processingPool.size:
                    for x in xrange(0, min(self.processingQ.qsize(), self.processingPool.free_count())):
                        self.processingPool.spawn(self.processingWorker)

                # If no more links in netQ, wait for processing to finish, check if there's any more links to process and if not, exit
                #if self.netQ.empty(): #and self.processingPool.free_count() == self.processingPool.size: ####### FIX, queue might commonly be emtpy, try checking pool counts instead
                if self.netPool.free_count() == self.netPool.size and self.processingPool.free_count() == self.processingPool.size:
                    print ''
                    for h in self.hit:
                        print h
                    sys.exit('All links: %d, Filtered: %d, percent filtered: %f, Runtime: %f' % (len(self.allLinks), self.filtered, float(self.filtered)/float(len(self.allLinks)), time.time() - start_time))

                gevent.sleep(0)

        except KeyboardInterrupt:
            print ''
            for h in self.hit:
                print h
            sys.exit('All links: %d, Filtered: %d, percent filtered: %f, Runtime: %s' % (len(self.allLinks), self.filtered, float(self.filtered)/float(len(self.allLinks)), time.time() - start_time))

    def netWorker(self, payload):
        ''' Fetchs the response to each link found '''
        # How to avoid opening links which just all redirect to the same page? append all found links
        # to a list maybe rather than just the post-redirected URL
        url, payload = self.netQ.get()
        try:
            resp = requests.get(url, headers = {'User-Agent':self.get_user_agent()}, timeout=30)
        except Exception as e:
            self.logger.error('Error opening %s: %s' % (url, str(e)))
            #print '[!] Error: %s\n          ' % url, e
            return

        #orig_url = url
        # Either add the resp to the queue, or test it for XSS
        if payload:
            self.processingQ.put((resp, payload))
        else:
            self.processingQ.put((resp, None))

    def processingWorker(self):
        ''' Parse the HTML and convert the links to usable URLs '''
        resp, payload = self.processingQ.get()

        if '/' == resp.url[:-1]:
            url = resp.url[:-1] # this gets the url in case of redirect and strips the last '/'
        else:
            url = resp.url
        url = resp.url
        self.logger.debug('Parsing %s' % url)

        # If seed url, save the root domain to confirm further links are hosted on the same domain
        if url == self.seed_url:
            self.root_domain = self.url_processor(url)[2]

        path = self.url_processor(url)[3]
        if path.startswith(self.base_path):
            html = resp.content
            if payload:
                self.xssChecker(url, html, payload)
            else:
                # Get links
                self.getLinks(html, url)

    def getLinks(self, html, url):
        hostname, protocol, path, root_domain = self.url_processor(url)
        raw_links = self.get_raw_links(html, url)
        if raw_links == None:
            self.logger.debug('No raw links found; ending processing: %s' % url)
            return

        # Only add links that are within scope
        if self.root_domain in hostname:
            parent_hostname = protocol+hostname
        else:
            return

        # Make sure to check the seed URL for XSS vectors
        if url == self.seed_url:
            self.checkForURLparams(url)
        
        link_exts = ['.ogg', '.flv', '.swf', '.mp3', '.jpg', '.jpeg', '.gif', '.css', '.ico', '.rss' '.tiff', '.png', '.pdf']
        for link in raw_links:
            link = self.filter_links(link, link_exts, parent_hostname)
            if link:
                linkSet = set()
                linkSet.add(link)
                path = self.url_processor(link)[3]
                if linkSet.issubset(self.allLinks):
                    continue
                if not path.startswith(self.base_path):
                    continue
                if "cat=" in link:
                    continue
                self.allLinks.add(link)
                self.netQ.put((link, None))
                print 'Link found:', link
                # Add the XSS payloaded links to the queue if it has variables in it
                self.checkForURLparams(link)

    def checkForURLparams(self, link):
        ''' Add links with variables in them to the queue again but with XSS testing payloads '''
        if '=' in link: # Check if there are URL parameters
            moddedParams = self.XSS.main(link)
            hostname, protocol, root_domain, path = self.url_processor(link)
            for payload in moddedParams:
                for params in moddedParams[payload]:
                    joinedParams = urllib.urlencode(params, doseq=1) # doseq maps the params back together
                    newLink = urllib.unquote(protocol+hostname+path+'?'+joinedParams)
                    #print newLink
                    self.netQ.put((newLink, payload))
            #print ''

    def xssChecker(self, url, html, payload):
        delim = self.XSS.xssDelim
        test = self.XSS.payloadTest
        testList = list(self.XSS.payloadTest)
        foundChars = []
        allFoundChars = []
        allBetweenDelims = '%s(.*?)%s' % (delim, delim)
        #allBetweenDelims = '%s(.{0,10})(%s)?' % (delim, delim) #This works too in case the second delim is missing, just add m=m[0]

        # Check if the test string is in the html, if so then it's very vulnerable
        # Some way to not run through html twice if nothing is found here?
        if test in html:
            print '100% vulnerable:', url
            self.hit.append(url)
            self.logfile.write('VULNERABLE: '+url+'\n')
            return

        # Check which characters show up unescaped
        matches = re.findall(allBetweenDelims, html)
        if len(matches) > 0:
            for m in matches:
                # Check for the special chars
                for c in testList:
                    if c in m:
                        foundChars.append(c)
                #print 'Found chars:', foundChars, url
                allFoundChars.append(foundChars)
                foundChars = []
            #print allFoundChars, url
            for chars in allFoundChars:
                if testList == chars:
                    print '100% vulnerable:', url
                    self.hit.append(url)
                    self.logfile.write('VULNERABLE: '+url+'\n')
                    return
                elif set(['"', '<', '>', '=', ')', '(']).issubset(set(chars)):
                    print 'Vulnerable! Try: "><svg onload=prompt(1) as the payload', url
                    self.hit.append(url)
                    self.logfile.write('VULNERABLE: '+url+'\n')
                #elif set([';', '(', ')']).issubset(set(chars)):
                #    print 'If this parameter is reflected in javascript, try payload: javascript:alert(1)', url
                #    self.hit.append(url)

            allFoundChars = []

    def url_processor(self, url):
        ''' Get the url domain, protocol, and hostname using urlparse '''
        try:
            parsed_url = urlparse(url)
            # Get the path
            path = parsed_url.path
            # Get the protocol
            protocol = parsed_url.scheme+'://'
            # Get the hostname (includes subdomains)
            hostname = parsed_url.hostname
            # Get root domain
            root_domain = '.'.join(hostname.split('.')[-2:])
        except:
            print '[-] Could not parse url:', url
            return

        return (hostname, protocol, root_domain, path)

    def get_raw_links(self, html, url):
        ''' Finds all links on the page lxml is faster than BeautifulSoup '''
        try:
            root = lxml.html.fromstring(html)
        except Exception:
            self.logger.error('[!] Failed to parse the html from %s' % url)
            return

        raw_links = [link[2] for link in root.iterlinks()]
        return raw_links

    def filter_links(self, link, link_exts, parent_hostname):
        link = link.strip()
        link = urllib.unquote(link)

        if len(link) > 0:
            # Filter out pages that aren't going to have links on them. Hacky.
            for ext in link_exts:
                if link.endswith(ext):
                    self.logger.debug('Filtered: '+link)
                    self.filtered +=1
                    return
            # Don't add links to scheduler with # since they're just JS or an anchor
            if link.startswith('#'):
                self.logger.debug('Filtered: '+link)
                self.filtered +=1
                return
            # Handle links like /articles/hello.html but not //:
            elif link.startswith('/') and ':' not in link:
                try:
                    link = parent_hostname+link.decode('utf-8')
                except Exception:
                    self.logger.error('Encoding error')
                    self.filtered +=1
                    return
                self.logger.debug('Appended: '+link)
            # Ignore links that are simple "http://"
            elif 'http://' == link.lower():
                self.logger.debug('Filtered: '+link)
                self.filtered +=1
                return
            # Handle full URL links
            elif link.lower().startswith('http'):
                link_hostname = urlparse(link).hostname
                if not link_hostname:
                    self.logger.error('Failed to get the hostname from this link: %s' % link)
                    return
                if self.root_domain in link_hostname:
                    self.logger.debug('Appended: '+link)
                else:
                    self.logger.debug('Filtered: '+link)
                    self.filtered +=1
                    return
            # Ignore links that don't start with http but still have : like android-app://com.tumblr
            # or javascript:something
            elif ':' in link:
                self.logger.debug('Filtered due to colon: '+link)
                self.filtered +=1
                return
            # Catch all unhandled URLs like "about/me.html" will go here
            else:
                link = parent_hostname+'/'+link
                self.logger.debug('Appended: '+link)

            return link

    def get_user_agent(self):
        '''
        Set the UA to be a random 1 of the top 6 most common
        '''
        user_agents = ['Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/34.0.1847.131 Safari/537.36',
                       'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/34.0.1847.131 Safari/537.36',
                       'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_9_2) AppleWebKit/537.75.14 (KHTML, like Gecko) Version/7.0.3 Safari/537.75.14',
                       'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:29.0) Gecko/20100101 Firefox/29.0',
                       'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/34.0.1847.137 Safari/537.36',
                       'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:28.0) Gecko/20100101 Firefox/28.0']
        user_agent = user_agents[randrange(5)]
        return user_agent

S = Spider(parse_args())
S.start()
