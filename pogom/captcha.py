#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
 - Captcha Overseer:
   - Tracks incoming new captcha tokens
   - Monitors the captcha'd accounts queue
   - Launches captcha_solver threads
 - Captcha Solver Threads each:
   - Have a unique captcha token
   - Attempts to verifyChallenge
   - Puts account back in active queue
   - Pushes webhook messages with captcha status
'''

import logging
import time
import requests

from datetime import datetime
from threading import Thread

from pgoapi import PGoApi
from .fakePogoApi import FakePogoApi
from .pgoapiwrapper import PGoApiWrapper

from .models import Token
from .transform import jitter_location
from .account import check_login
from .proxy import get_new_proxy
from .utils import now


log = logging.getLogger(__name__)


def captcha_overseer_thread(args, account_queue, account_captchas,
                            key_scheduler, wh_queue):
    solverId = 0
    while True:
        # Run once every 15 seconds.
        sleep_timer = 15

        tokens_needed = len(account_captchas)
        if tokens_needed > 0:
            tokens = Token.get_valid(tokens_needed)
            tokens_available = len(tokens)
            solvers = min(tokens_needed, tokens_available)
            log.debug('Captcha overseer running. Captchas: %d - Tokens: %d',
                      tokens_needed, tokens_available)
            for i in range(0, solvers):
                hash_key = None
                if args.hash_key:
                    hash_key = key_scheduler.next()

                t = Thread(target=captcha_solver_thread,
                           name='captcha-solver-{}'.format(solverId),
                           args=(args, account_queue, account_captchas,
                                 hash_key, wh_queue, tokens[i]))
                t.daemon = True
                t.start()

                solverId += 1
                if solverId > 999:
                    solverId = 0
                # Wait a bit before launching next thread
                time.sleep(1)

            # Adjust captcha-overseer sleep timer
            sleep_timer -= 1 * solvers

            # Hybrid mode
            if args.captcha_key and args.manual_captcha_timeout > 0:
                tokens_remaining = tokens_needed - tokens_available
                # Safety guard
                tokens_remaining = min(tokens_remaining, 5)
                for i in range(0, tokens_remaining):
                    account = account_captchas[0][1]
                    last_active = account['last_active']
                    hold_time = (datetime.utcnow() -
                                 last_active).total_seconds()
                    if hold_time > args.manual_captcha_timeout:
                        log.debug('Account %s waited %ds for captcha token ' +
                                  'and reached the %ds timeout.',
                                  account['username'], hold_time,
                                  args.manual_captcha_timeout)
                        if args.hash_key:
                            hash_key = key_scheduler.next()

                        t = Thread(target=captcha_solver_thread,
                                   name='captcha-solver-{}'.format(solverId),
                                   args=(args, account_queue, account_captchas,
                                         hash_key, wh_queue))
                        t.daemon = True
                        t.start()

                        solverId += 1
                        if solverId > 999:
                            solverId = 0
                        # Wait a bit before launching next thread
                        time.sleep(1)
                    else:
                        break

        time.sleep(sleep_timer)


def captcha_solver_thread(args, account_queue, account_captchas, hash_key,
                          wh_queue, token=None):
    status, account, captcha_url = account_captchas.popleft()

    status['message'] = 'Waking up account {} to verify captcha token.'.format(
                         account['username'])
    log.info(status['message'])

    if args.mock != '':
        api = FakePogoApi(args.mock)
    else:
        api = PGoApiWrapper(PGoApi())

    if hash_key:
        log.debug('Using key {} for solving this captcha.'.format(hash_key))
        api.activate_hash_server(hash_key)

    proxy_url = False
    if args.proxy:
        # Try to fetch a new proxy.
        proxy_num, proxy_url = get_new_proxy(args)

        if proxy_url:
            log.debug('Using proxy %s', proxy_url)
            api.set_proxy({'http': proxy_url, 'https': proxy_url})

    location = account['last_location']

    if args.jitter:
        # Jitter location before uncaptcha attempt.
        location = jitter_location(location)

    api.set_position(*location)
    check_login(args, account, api, proxy_url)

    if not token:
        token = token_request(args, status, captcha_url)

    req = api.create_request()
    req.verify_challenge(token=token)
    response = req.call(False)

    last_active = account['last_active']
    hold_time = (datetime.utcnow() - last_active).total_seconds()

    success = response['responses']['VERIFY_CHALLENGE'].success
    if success:
        status['message'] = (
            "Account {} successfully uncaptcha'd, returning to " +
            'active duty.').format(account['username'])
        log.info(status['message'])
        account_queue.put(account)
    else:
        status['message'] = (
            'Account {} failed verifyChallenge, putting back ' +
            'in captcha queue.').format(account['username'])
        log.warning(status['message'])
        account_captchas.append((status, account, captcha_url))

    if 'captcha' in args.wh_types:
        wh_message = {
            'status_name': args.status_name,
            'mode': 'manual' if token else '2captcha',
            'account': account['username'],
            'captcha': status['captcha'],
            'time': int(hold_time),
            'status': 'success' if success else 'failure'
        }
        wh_queue.put(('captcha', wh_message))
    # Make sure status is updated
    time.sleep(1)


def handle_captcha(args, status, api, account, account_failures,
                   account_captchas, whq, response_dict, step_location):
    if 'CHECK_CHALLENGE' not in response_dict['responses']:
        return None

    captcha_url = response_dict['responses']['CHECK_CHALLENGE'].challenge_url

    if len(captcha_url) > 1:
        status['captcha'] += 1
        if not args.captcha_solving:
            status['message'] = (
                'Account {} has encountered a captcha. ' +
                'Putting account away.').format(account['username'])
            log.warning(status['message'])
            account_failures.append({
                'account': account,
                'last_fail_time': now(),
                'reason': 'captcha found'
            })
            if 'captcha' in args.wh_types:
                wh_message = {
                    'status_name': args.status_name,
                    'status': 'encounter',
                    'mode': 'disabled',
                    'account': account['username'],
                    'captcha': status['captcha'],
                    'time': 0
                }
                whq.put(('captcha', wh_message))
            return False

        if args.captcha_key and args.manual_captcha_timeout == 0:
            if automatic_captcha_solve(args, status, api, captcha_url, account,
                                       whq):
                return True
            else:
                account_failures.append({
                    'account': account,
                    'last_fail_time': now(),
                    'reason': 'captcha failed to verify'
                })
                return False
        else:
            status['message'] = (
                'Account {} has encountered a captcha. ' +
                'Waiting for token.').format(account['username'])
            log.warning(status['message'])
            account['last_active'] = datetime.utcnow()
            account['last_location'] = step_location
            account_captchas.append((status, account, captcha_url))
            if 'captcha' in args.wh_types:
                wh_message = {
                    'status_name': args.status_name,
                    'status': 'encounter',
                    'mode': 'manual',
                    'account': account['username'],
                    'captcha': status['captcha'],
                    'time': args.manual_captcha_timeout
                }
                whq.put(('captcha', wh_message))
            return False

    return None


# Return True if captcha was succesfully solved
def automatic_captcha_solve(args, status, api, captcha_url, account, wh_queue):
    status['message'] = (
        'Account {} is encountering a captcha, starting 2captcha ' +
        'sequence.').format(account['username'])
    log.warning(status['message'])

    if 'captcha' in args.wh_types:
        wh_message = {'status_name': args.status_name,
                      'status': 'encounter',
                      'mode': '2captcha',
                      'account': account['username'],
                      'captcha': status['captcha'],
                      'time': 0}
        wh_queue.put(('captcha', wh_message))

    time_start = now()
    captcha_token = token_request(args, status, captcha_url)
    time_elapsed = now() - time_start

    if 'ERROR' in captcha_token:
        log.warning('Unable to resolve captcha, please check your ' +
                    '2captcha API key and/or wallet balance.')
        if 'captcha' in args.wh_types:
            wh_message['status'] = 'error'
            wh_message['time'] = time_elapsed
            wh_queue.put(('captcha', wh_message))

        return False
    else:
        status['message'] = (
            'Retrieved captcha token, attempting to verify challenge ' +
            'for {}.').format(account['username'])
        log.info(status['message'])

        req = api.create_request()
        req.verify_challenge(token=captcha_token)
        response = req.call(False)
        time_elapsed = now() - time_start
        success = response['responses']['VERIFY_CHALLENGE'].success
        if success:
            status['message'] = "Account {} successfully uncaptcha'd.".format(
                account['username'])
        else:
            status['message'] = (
                'Account {} failed verifyChallenge, putting away ' +
                'account for now.').format(account['username'])
        log.info(status['message'])
        if 'captcha' in args.wh_types:
            wh_message['status'] = 'success' if success else 'failure'
            wh_message['time'] = time_elapsed
            wh_queue.put(('captcha', wh_message))

        return success


def token_request(args, status, url):
    s = requests.Session()
    # Fetch the CAPTCHA_ID from 2captcha.
    try:
        request_url = (
            'http://2captcha.com/in.php?key={}&method=userrecaptcha' +
            '&googlekey={}&pageurl={}').format(args.captcha_key,
                                               args.captcha_dsk, url)
        captcha_id = s.post(request_url, timeout=5).text.split('|')[1]
        captcha_id = str(captcha_id)
    # IndexError implies that the retuned response was a 2captcha error.
    except IndexError:
        return 'ERROR'
    status['message'] = (
        'Retrieved captcha ID: {}; now retrieving token.').format(captcha_id)
    log.info(status['message'])
    # Get the response, retry every 5 seconds if it's not ready.
    recaptcha_response = s.get(
        'http://2captcha.com/res.php?key={}&action=get&id={}'.format(
            args.captcha_key, captcha_id), timeout=5).text
    while 'CAPCHA_NOT_READY' in recaptcha_response:
        log.info('Captcha token is not ready, retrying in 5 seconds...')
        time.sleep(5)
        recaptcha_response = s.get(
            'http://2captcha.com/res.php?key={}&action=get&id={}'.format(
                args.captcha_key, captcha_id), timeout=5).text
    token = str(recaptcha_response.split('|')[1])
    return token
