"""Names shared by the current complete-toolkit build and release pipeline."""
from workbench import __version__

VERSION = __version__
TAG = f'v{VERSION}'
ACCEPTANCE_BRANCH = 'fix/workspace-privacy-0.6.2'
PUBLISH_BRANCH = f'release/validated-{VERSION}'
ACCEPTANCE_WORKFLOW = '.github/workflows/desktop-acceptance.yml'
WINDOWS_DIRECTORY = f'Figure_Workbench_{VERSION}_Windows_x64'
SOURCE_DIRECTORY = f'Figure_Workbench_{VERSION}_Source'
FULL = f'Figure_Workbench_{VERSION}_Windows_Full.zip'
SOURCE = f'{SOURCE_DIRECTORY}.zip'
BROWSER = f'Figure_Workbench_{VERSION}_Browser.html'
CPU_INTERMEDIATE = f'Figure_Workbench_{VERSION}_Cellpose_CPU.zip'
QA = f'Figure_Workbench_{VERSION}_QA_Evidence.zip'
RECEIPT = 'RELEASE_ACCEPTANCE.json'
CHECKSUMS = 'SHA256SUMS'
PACKAGES = frozenset({FULL, SOURCE, BROWSER})
ATTACHMENTS = PACKAGES | {QA, RECEIPT, CHECKSUMS}

# These version-specific behaviors must execute in the packaged native client;
# an unrelated successful scenario cannot stand in for the current changes.
NATIVE_REQUIRED_SCENARIOS = frozenset({
    'native client startup',
    'native composition parameters apply without confirmation and retain atomic undo',
    'restart requires explicit recovery before restoring the saved sources and analysis',
    'native workspace clearing deletes owned copies and preserves external files',
    'cleared native workspace stays empty after restart',
})
