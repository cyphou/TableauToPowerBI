"""
Migration configuration file support.

Supports JSON configuration files for repeatable migrations.
No external dependencies required (uses Python stdlib json).

Example configuration file (migration_config.json):
{
    "source": {
        "tableau_file": "workbook.twbx",
        "prep_flow": null
    },
    "output": {
        "directory": "artifacts/powerbi_projects",
        "format": "pbip",
        "report_name": null
    },
    "model": {
        "mode": "auto",
        "culture": "en-US",
        "calendar_start": 2020,
        "calendar_end": 2030
    },
    "connections": {
        "template_vars": {
            "SERVER": "prod-db.company.com",
            "DATABASE": "analytics",
            "PORT": "5432"
        }
    },
    "migration": {
        "skip_extraction": false,
        "skip_conversion": false,
        "hyper_export_format": "auto",
        "hyper_export_max_rows": null,
        "max_zip_entry_mb": null,
        "dry_run": false,
        "rollback": false,
        "verbose": false,
        "log_file": null
    },
    "plugins": []
}
"""

import copy
import json
import logging
import os

logger = logging.getLogger('tableau_to_powerbi.config')


# Default configuration values
_DEFAULTS = {
    'source': {
        'tableau_file': None,
        'prep_flow': None,
    },
    'output': {
        'directory': None,
        'format': 'pbip',
        'report_name': None,
    },
    'model': {
        'mode': 'auto',
        'culture': 'en-US',
        'calendar_start': 2020,
        'calendar_end': 2030,
    },
    'connections': {
        'template_vars': {},
    },
    'migration': {
        'skip_extraction': False,
        'skip_conversion': False,
        'dry_run': False,
        'rollback': False,
        'verbose': False,
        'log_file': None,
        'max_zip_entry_mb': None,
        'hyper_export_format': 'auto',
        'hyper_export_max_rows': None,
    },
    'plugins': [],
}


class MigrationConfig:
    """Configuration container for migration settings.

    Supports loading from JSON files and merging with CLI arguments.
    CLI arguments always take precedence over config file values.
    """

    def __init__(self, config_dict=None):
        """Initialize with optional configuration dictionary.

        Args:
            config_dict: Dict with configuration values (missing keys use defaults)
        """
        self._config = copy.deepcopy(_DEFAULTS)
        if config_dict:
            incoming = copy.deepcopy(config_dict)
            # Continue accepting the flat shape used by config.example.json
            # while keeping the nested schema as the canonical representation.
            flat_fields = {
                'tableau_file': ('source', 'tableau_file'),
                'prep_flow': ('source', 'prep_flow'),
                'output_dir': ('output', 'directory'),
                'output_format': ('output', 'format'),
                'report_name': ('output', 'report_name'),
                'model_mode': ('model', 'mode'),
                'mode': ('model', 'mode'),
                'culture': ('model', 'culture'),
                'calendar_start': ('model', 'calendar_start'),
                'calendar_end': ('model', 'calendar_end'),
                'skip_extraction': ('migration', 'skip_extraction'),
                'skip_conversion': ('migration', 'skip_conversion'),
                'dry_run': ('migration', 'dry_run'),
                'rollback': ('migration', 'rollback'),
                'verbose': ('migration', 'verbose'),
                'log_file': ('migration', 'log_file'),
                'max_zip_entry_mb': ('migration', 'max_zip_entry_mb'),
                'hyper_export_format': ('migration', 'hyper_export_format'),
                'hyper_export_max_rows': ('migration', 'hyper_export_max_rows'),
            }
            for field, (section, key) in flat_fields.items():
                if field not in incoming:
                    continue
                value = incoming.pop(field)
                section_values = incoming.setdefault(section, {})
                if not isinstance(section_values, dict):
                    raise ValueError(f"Configuration section '{section}' must be an object")
                section_values.setdefault(key, value)
            self._merge(self._config, incoming)

    @classmethod
    def from_file(cls, filepath):
        """Load configuration from a JSON file.

        Args:
            filepath: Path to a JSON configuration file

        Returns:
            MigrationConfig: Loaded configuration

        Raises:
            FileNotFoundError: If config file doesn't exist
            json.JSONDecodeError: If config file has invalid JSON
        """
        abs_path = os.path.abspath(filepath)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Configuration file not found: {abs_path}")

        with open(abs_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        logger.info(f"Loaded configuration from: {abs_path}")
        return cls(data)

    @classmethod
    def from_args(cls, args):
        """Create configuration from argparse Namespace.

        Maps CLI arguments to their configuration sections.
        Only non-None values are set (preserving defaults).

        Args:
            args: argparse.Namespace with CLI arguments

        Returns:
            MigrationConfig: Configuration from CLI args
        """
        data = copy.deepcopy(_DEFAULTS)

        # Source
        if hasattr(args, 'tableau_file') and args.tableau_file:
            data['source']['tableau_file'] = args.tableau_file
        if hasattr(args, 'prep') and args.prep:
            data['source']['prep_flow'] = args.prep

        # Output
        if hasattr(args, 'output_dir') and args.output_dir:
            data['output']['directory'] = args.output_dir
        if hasattr(args, 'output_format') and args.output_format:
            data['output']['format'] = args.output_format

        # Model
        if hasattr(args, 'mode') and args.mode is not None:
            data['model']['mode'] = args.mode
        if hasattr(args, 'culture') and args.culture:
            data['model']['culture'] = args.culture
        if hasattr(args, 'calendar_start') and args.calendar_start is not None:
            data['model']['calendar_start'] = args.calendar_start
        if hasattr(args, 'calendar_end') and args.calendar_end is not None:
            data['model']['calendar_end'] = args.calendar_end

        # Migration options
        if hasattr(args, 'skip_extraction') and args.skip_extraction:
            data['migration']['skip_extraction'] = True
        if hasattr(args, 'skip_conversion') and args.skip_conversion:
            data['migration']['skip_conversion'] = True
        if hasattr(args, 'dry_run') and args.dry_run:
            data['migration']['dry_run'] = True
        if hasattr(args, 'rollback') and args.rollback:
            data['migration']['rollback'] = True
        if hasattr(args, 'verbose') and args.verbose:
            data['migration']['verbose'] = True
        if hasattr(args, 'log_file') and args.log_file:
            data['migration']['log_file'] = args.log_file
        for key in (
                'max_zip_entry_mb', 'hyper_export_format',
                'hyper_export_max_rows'):
            value = getattr(args, key, None)
            if value is not None:
                data['migration'][key] = value

        return cls(data)

    def merge_with_args(self, args):
        """Merge CLI arguments over this config (CLI wins).

        Args:
            args: argparse.Namespace with CLI arguments

        Returns:
            MigrationConfig: New config with CLI overrides applied
        """
        cli_config = MigrationConfig.from_args(args)
        result = copy.deepcopy(self._config)
        self._merge_nondefault(result, cli_config._config, _DEFAULTS)
        # ``auto`` is the default mode too, so compare against parser None to
        # retain the CLI's precedence when the user explicitly selects auto.
        if getattr(args, 'mode', None) is not None:
            result['model']['mode'] = args.mode
        for key in (
                'max_zip_entry_mb', 'hyper_export_format',
                'hyper_export_max_rows'):
            value = getattr(args, key, None)
            if value is not None:
                result['migration'][key] = value
        return MigrationConfig(result)

    @staticmethod
    def _merge(target, source):
        """Deep-merge source dict into target dict."""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                MigrationConfig._merge(target[key], value)
            else:
                target[key] = value

    @staticmethod
    def _merge_nondefault(target, source, defaults):
        """Merge source into target, but only where source differs from defaults."""
        for key, value in source.items():
            if isinstance(value, dict) and key in target and isinstance(target[key], dict):
                default_sub = defaults.get(key, {}) if isinstance(defaults.get(key), dict) else {}
                MigrationConfig._merge_nondefault(target[key], value, default_sub)
            else:
                # Only override if value differs from default
                default_val = defaults.get(key)
                if value != default_val:
                    target[key] = value

    # ── Property accessors ────────────────────────────────────

    @property
    def tableau_file(self):
        return self._config['source']['tableau_file']

    @property
    def prep_flow(self):
        return self._config['source']['prep_flow']

    @property
    def output_dir(self):
        return self._config['output']['directory']

    @property
    def output_format(self):
        return self._config['output']['format']

    @property
    def report_name(self):
        return self._config['output']['report_name']

    @property
    def model_mode(self):
        return self._config['model']['mode']

    @property
    def culture(self):
        return self._config['model']['culture']

    @property
    def calendar_start(self):
        return self._config['model']['calendar_start']

    @property
    def calendar_end(self):
        return self._config['model']['calendar_end']

    @property
    def template_vars(self):
        return self._config['connections'].get('template_vars', {})

    @property
    def skip_extraction(self):
        return self._config['migration']['skip_extraction']

    @property
    def dry_run(self):
        return self._config['migration']['dry_run']

    @property
    def rollback(self):
        return self._config['migration']['rollback']

    @property
    def verbose(self):
        return self._config['migration']['verbose']

    @property
    def log_file(self):
        return self._config['migration']['log_file']

    @property
    def max_zip_entry_mb(self):
        return self._config['migration']['max_zip_entry_mb']

    @property
    def hyper_export_format(self):
        return self._config['migration']['hyper_export_format']

    @property
    def hyper_export_max_rows(self):
        return self._config['migration']['hyper_export_max_rows']

    @property
    def plugins(self):
        return self._config.get('plugins', [])

    def to_dict(self):
        """Return a deep copy of the configuration dictionary."""
        return copy.deepcopy(self._config)

    def save(self, filepath):
        """Save configuration to a JSON file.

        Args:
            filepath: Path to write the configuration file
        """
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._config, f, indent=2, ensure_ascii=False)
        logger.info(f"Configuration saved to: {filepath}")


def load_config(filepath=None, args=None):
    """Load configuration from file and/or CLI arguments.

    Priority: CLI args > config file > defaults

    Args:
        filepath: Optional path to JSON config file
        args: Optional argparse.Namespace with CLI arguments

    Returns:
        MigrationConfig: Merged configuration
    """
    if filepath:
        config = MigrationConfig.from_file(filepath)
        if args:
            config = config.merge_with_args(args)
        return config
    elif args:
        return MigrationConfig.from_args(args)
    else:
        return MigrationConfig()
