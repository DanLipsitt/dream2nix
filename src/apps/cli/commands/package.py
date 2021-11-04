import json
import os
import re
import subprocess as sp
import sys
import tempfile

import networkx as nx
from cleo import Command, option

from utils import dream2nix_src, checkLockJSON, callNixFunction, buildNixFunction, buildNixAttribute, \
  list_translators_for_source, strip_hashes_from_lock


class PackageCommand(Command):

  description = (
    "Package a software project using nix"
  )

  name = "package"

  options = [
    option(
      "source",
      None,
      "source of the package, can be a path or flake-like spec",
      flag=False,
      multiple=False
    ),
    option("translator", None, "which translator to use", flag=False),
    option("output", None, "output file/directory for the dream-lock.json", flag=False),
    option(
      "combined",
      None,
      "store only one hash for all sources combined"
      " (smaller lock file but larger FOD)",
      flag=True
    ),
    option(
      "arg",
      None,
      "extra arguments for selected translator",
      flag=False,
      multiple=True
    ),
    option("force", None, "override existing files", flag=True),
    option("default-nix", None, "create default.nix", flag=True),
  ]

  def handle(self):
    if self.io.is_interactive():
      self.line(f"\n{self.description}\n")

    # parse extra args
    specified_extra_args = {
      arg[0]: arg[1] for arg in map(
        lambda e: e.split('='),
        self.option("arg"),
      )
    }

    # ensure output directory
    output = self.option("output")
    if not output:
      output = './.'
    if not os.path.isdir(output):
      os.mkdir(output)
    filesToCreate = ['dream-lock.json']
    if self.option('default-nix'):
      filesToCreate.append('default.nix')
    if self.option('force'):
      for f in filesToCreate:
        if os.path.isfile(f):
          os.remove(f)
    else:
      existingFiles = set(os.listdir(output))
      if any(f in existingFiles for f in filesToCreate):
        print(
          f"output directory {output} already contains a 'default.nix' "
          "or 'dream-lock.json'. Delete first, or user '--force'.",
          file=sys.stderr,
        )
        exit(1)
    output = os.path.realpath(output)
    outputDreamLock = f"{output}/dream-lock.json"
    outputDefaultNix = f"{output}/default.nix"

    # verify source
    source = self.option("source")
    if not source:
      source = os.path.realpath('./.')
      print(
        f"Source not specified. Defaulting to current directory: {source}",
        file=sys.stderr,
      )
    # check if source is valid fetcher spec
    sourceSpec = {}
    # handle source shortcuts
    if source.partition(':')[0].split('+')[0] in os.environ.get("fetcherNames", None).split()\
        or source.startswith('http'):
      print(f"fetching source for '{source}'")
      sourceSpec =\
        callNixFunction("fetchers.translateShortcut", shortcut=source)
      source =\
        buildNixFunction("fetchers.fetchShortcut", shortcut=source, extract=True)
    # handle source paths
    else:
      # check if source path exists
      if not os.path.exists(source):
        print(f"Input source '{source}' does not exist", file=sys.stdout)
        exit(1)
      source = os.path.realpath(source)
      # handle source from dream-lock.json
      if source.endswith('dream-lock.json'):
        print(f"fetching source defined via existing dream-lock.json")
        with open(source) as f:
          sourceDreamLock = json.load(f)
        sourceMainPackageName = sourceDreamLock['_generic']['mainPackageName']
        sourceMainPackageVersion = sourceDreamLock['_generic']['mainPackageVersion']
        sourceSpec =\
          sourceDreamLock['sources'][sourceMainPackageName][sourceMainPackageVersion]
        source = \
          buildNixFunction("fetchers.fetchSource", source=sourceSpec, extract=True)

    # select translator
    translatorsSorted = sorted(
      list_translators_for_source(source),
      key=lambda t: (
        not t['compatible'],
        ['pure', 'ifd', 'impure'].index(t['type'])
      )
    )
    translator = self.option("translator")
    if not translator:
      chosen = self.choice(
        'Select translator',
        list(map(
          lambda t: f"{t['subsystem']}.{t['type']}.{t['name']}{'  (compatible)' if t['compatible'] else ''}",
          translatorsSorted
        )),
        0
      )
      translator = chosen
      translator = list(filter(
        lambda t: [t['subsystem'], t['type'], t['name']] == translator.split('  (')[0].split('.'),
        translatorsSorted,
      ))[0]
    else:
      translator = translator.split('.')
      try:
        if len(translator) == 3:
          translator = list(filter(
            lambda t: [t['subsystem'], t['type'], t['name']] == translator,
            translatorsSorted,
          ))[0]
        elif len(translator) == 1:
          translator = list(filter(
            lambda t:  [t['name']] == translator,
            translatorsSorted,
          ))[0]
      except IndexError:
        print(f"Could not find translator '{'.'.join(translator)}'", file=sys.stderr)
        exit(1)

    # raise error if any specified extra arg is unknown
    unknown_extra_args = set(specified_extra_args.keys()) - set(translator['extraArgs'].keys())
    if unknown_extra_args:
      print(
        f"Invalid extra args for translator '{translator['name']}': "
        f" {', '.join(unknown_extra_args)}"
        "\nPlease remove these parameters",
        file=sys.stderr
      )
      exit(1)

    # transform flags to bool
    for argName, argVal in specified_extra_args.copy().items():
      if translator['extraArgs'][argName]['type'] == 'flag':
        if argVal.lower() in ('yes', 'y', 'true'):
          specified_extra_args[argName] = True
        elif argVal.lower() in ('no', 'n', 'false'):
          specified_extra_args[argName] = False
        else:
          print(
            f"Invalid value {argVal} for argument {argName}",
            file=sys.stderr
          )

    specified_extra_args =\
      {k: (bool(v) if translator['extraArgs'][k]['type'] == 'flag' else v ) \
          for k, v in specified_extra_args.items()}

    # on non-interactive session, assume defaults for unspecified extra args
    if not self.io.is_interactive():
      specified_extra_args.update(
        {n: (True if v['type'] == 'flag' else v['default']) \
          for n, v in translator['extraArgs'].items() \
          if n not in specified_extra_args and 'default' in v}
      )
    unspecified_extra_args = \
      {n: v for n, v in translator['extraArgs'].items() \
        if n not in specified_extra_args}
    # raise error if any extra arg unspecified in non-interactive session
    if unspecified_extra_args:
      if not self.io.is_interactive():
        print(
          f"Please specify the following extra arguments required by translator '{translator['name']}' :\n" \
            ', '.join(unspecified_extra_args.keys()),
          file=sys.stderr
        )
        exit(1)
      # interactively retrieve answers for unspecified extra arguments
      else:
        print(f"\nThe translator '{translator['name']}' requires additional options")
        for arg_name, arg in unspecified_extra_args.items():
          print('')
          if arg['type'] == 'flag':
            print(f"Please specify '{arg_name}'")
            specified_extra_args[arg_name] = self.confirm(f"{arg['description']}:", False)
          else:
            print(f"Please specify '{arg_name}': {arg['description']}")
            print(f"Example values: " + ', '.join(arg['examples']))
            if 'default' in arg:
              print(f"Leave empty for default ({arg['default']})")
            while True:
              specified_extra_args[arg_name] = self.ask(f"{arg_name}:", arg.get('default'))
              if specified_extra_args[arg_name]:
                break

    # arguments for calling the translator nix module
    translator_input = dict(
      inputFiles=[],
      inputDirectories=[source],
      outputFile=outputDreamLock,
    )
    translator_input.update(specified_extra_args)

    # build the translator bin
    t = translator
    translator_path = buildNixAttribute(
      f"translators.translators.{t['subsystem']}.{t['type']}.{t['name']}.translateBin"
    )

    # dump translator arguments to json file and execute translator
    print("\nTranslating upstream metadata")
    with tempfile.NamedTemporaryFile("w") as input_json_file:
      json.dump(translator_input, input_json_file, indent=2)
      input_json_file.seek(0) # flushes write cache

      # execute translator
      sp.run(
        [f"{translator_path}/bin/run", input_json_file.name]
      )

    # raise error if output wasn't produced
    if not os.path.isfile(outputDreamLock):
      raise Exception(f"Translator failed to create dream-lock.json")

    # read produced lock file
    with open(outputDreamLock) as f:
      lock = json.load(f)

    # write translator information to lock file
    combined = self.option('combined')
    lock['_generic']['translatedBy'] = f"{t['subsystem']}.{t['type']}.{t['name']}"
    lock['_generic']['translatorParams'] = " ".join([
      '--translator',
      f"{translator['subsystem']}.{translator['type']}.{translator['name']}",
    ] + (
      ["--combined"] if combined else []
    ) + [
      f"--arg {n}={v}" for n, v in specified_extra_args.items()
    ])

    # add main package source
    mainPackageName = lock['_generic']['mainPackageName']
    mainPackageVersion = lock['_generic']['mainPackageVersion']
    mainSource = sourceSpec.copy()
    if not mainSource:
      mainSource = dict(
        type="unknown",
      )
    if mainPackageName not in lock['sources']:
      lock['sources'][mainPackageName] = {
        mainPackageVersion: mainSource
      }
    else:
      lock['sources'][mainPackageName][mainPackageVersion] = mainSource

    # clean up dependency graph
    # remove empty entries
    if 'dependencies' in lock['_generic']:
      depGraph = lock['_generic']['dependencies']
      if 'dependencies' in lock['_generic']:
        for pname, deps in depGraph.copy().items():
          if not deps:
            del depGraph[pname]

      # remove cyclic dependencies
      edges = set()
      for pname, versions in depGraph.items():
        for version, deps in versions.items():
          for dep in deps:
            edges.add(((pname, version), tuple(dep)))
      G = nx.DiGraph(sorted(list(edges)))
      cycle_count = 0
      removed_edges = []
      for pname, versions in depGraph.items():
        for version in versions.keys():
          key = (pname, version)
          try:
            while True:
              cycle = nx.find_cycle(G, key)
              cycle_count += 1
              # remove_dependecy(indexed_pkgs, G, cycle[-1][0], cycle[-1][1])
              node_from, node_to = cycle[-1][0], cycle[-1][1]
              G.remove_edge(node_from, node_to)
              removed_edges.append((node_from, node_to))
          except nx.NetworkXNoCycle:
            continue
      lock['cyclicDependencies'] = {}
      if removed_edges:
        cycles_text = 'Detected Cyclic dependencies:'
        for node, removed in removed_edges:
          n_name, n_ver = node[0], node[1]
          r_name, r_ver = removed[0], removed[1]
          cycles_text +=\
            f"\n  {n_name}#{n_ver} -> {r_name}#{r_ver}"
          if n_name not in lock['cyclicDependencies']:
            lock['cyclicDependencies'][n_name] = {}
          if n_ver not in lock['cyclicDependencies'][n_name]:
            lock['cyclicDependencies'][n_name][n_ver] = []
          lock['cyclicDependencies'][n_name][n_ver].append(removed)
        print(cycles_text)

    # calculate combined hash if --combined was specified
    if combined:

      print("Building FOD of combined sources to retrieve output hash")

      # remove hashes from lock file and init sourcesCombinedHash with empty string
      strip_hashes_from_lock(lock)
      lock['_generic']['sourcesCombinedHash'] = ""
      with open(outputDreamLock, 'w') as f:
        json.dump(lock, f, indent=2)

      # compute FOD hash of combined sources
      proc = sp.run(
        [
          "nix", "build", "--impure", "-L", "--expr",
          f"(import {dream2nix_src} {{}}).fetchSources {{ dreamLock = {outputDreamLock}; }}"
        ],
        capture_output=True,
      )

      # read the output hash from the failed build log
      match = re.search(r"FOD_PATH=(.*=)", proc.stderr.decode())
      if not match:
        print(proc.stderr.decode())
        print(proc.stdout.decode())
        raise Exception("Could not find FOD hash in FOD log")
      hash = match.groups()[0]
      print(f"Computed FOD hash: {hash}")

      # store the hash in the lock
      lock['_generic']['sourcesCombinedHash'] = hash

    # re-write dream-lock.json
    checkLockJSON(lock)
    lockStr = json.dumps(lock, indent=2, sort_keys = True)
    lockStr = lockStr\
      .replace("[\n          ", "[ ")\
      .replace("\"\n        ]", "\" ]")\
      .replace(",\n          ", ", ")
    with open(outputDreamLock, 'w') as f:
      f.write(lockStr)

    # create default.nix
    template = callNixFunction(
      'apps.apps.cli.templateDefaultNix',
      dream2nixLocationRelative=os.path.relpath(dream2nix_src, output),
      dreamLock = lock,
      sourcePathRelative = os.path.relpath(source, os.path.dirname(outputDefaultNix))
    )
    # with open(f"{dream2nix_src}/apps/cli2/templateDefault.nix") as template:
    if self.option('default-nix'):
      with open(outputDefaultNix, 'w') as defaultNix:
        defaultNix.write(template)
        print(f"Created {output}/default.nix")

    print(f"Created {output}/dream-lock.json")