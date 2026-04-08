"""Wrapper around crawler agent for use in PRO server."""
import sys
import os


def run_auto_add(url: str, site_id: str = None, site_name: str = None,
                 browser: bool = False, max_iterations: int = 10) -> dict:
    """Run the AutoAddAgent and return result dict."""
    # Ensure crawler module is importable
    crawler_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if crawler_root not in sys.path:
        sys.path.insert(0, crawler_root)

    from crawler.agent import AutoAddAgent

    agent = AutoAddAgent(
        max_iterations=max_iterations,
        verbose=True,
        force_browser=browser,
    )
    return agent.run(url, site_id=site_id, site_name=site_name)
