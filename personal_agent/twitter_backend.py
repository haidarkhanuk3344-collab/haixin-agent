"""Isolated twitter-cli entry: fixed read commands and one explicitly chosen profile.

Run with work/twitter-runtime/bin/python, never the application's system Python.
No credentials are printed or stored by this wrapper; third-party stderr is discarded
by the parent. No posting, following, likes, bookmarks, or home timeline entry points.
"""
import json
import os
import sys


def main():
    mode,value,browser,profile=sys.argv[1:]
    if mode not in ('status','user','search') or browser not in ('ego','chrome','edge','brave','arc'):
        raise SystemExit(2)
    import twitter_cli.auth as auth
    import twitter_cli.cli as native
    if browser=='ego':
        secret=json.loads(sys.stdin.buffer.read(16385))
        if not isinstance(secret,dict) or set(secret)!={'auth_token','ct0'}:raise SystemExit(2)
        auth.load_from_env=lambda:dict(secret)
        auth._get_browser_order=lambda:[]
    else:
        import browser_cookie3
        from http.cookiejar import CookieJar
        browser_loader=getattr(browser_cookie3,browser)
        def twitter_cookies_only(*args,**kwargs):
            jar=CookieJar()
            for domain in ('x.com','twitter.com'):
                for cookie in browser_loader(*args,**dict(kwargs,domain_name=domain)):
                    if auth._is_twitter_domain(cookie.domain):jar.set_cookie(cookie)
            return jar
        setattr(browser_cookie3,browser,twitter_cookies_only)
        # Avoid scanning other browser profiles, and avoid native last-results cache.
        auth._get_browser_order=lambda:[browser]
        auth.load_from_env=lambda:None
        os.environ['TWITTER_CHROME_PROFILE']=profile
        find_cookies=auth._iter_chrome_cookie_files
        def exact_profile(name):
            paths=find_cookies(name)
            if not paths:raise RuntimeError('Selected browser profile is unavailable')
            return paths
        auth._iter_chrome_cookie_files=exact_profile
    native.save_tweet_cache=lambda _:None
    loader=native.load_config
    def limited_config(*args,**kwargs):
        cfg=loader(*args,**kwargs);cfg['fetch']['count']=20
        cfg['rateLimit'].update(maxRetries=0,maxCount=20,requestDelay=2.5)
        return cfg
    native.load_config=limited_config
    args=['status','--json'] if mode=='status' else ['user-posts','--max','20','--json','--',value] if mode=='user' else ['search','--max','20','--json','-t','Latest','--',value]
    native.cli(args=args)


if __name__=='__main__':main()
