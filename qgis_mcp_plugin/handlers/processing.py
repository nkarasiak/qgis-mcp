"""Handlers for the QGIS Processing framework: algorithms, models, analysis."""

import contextlib
import os
import time
from typing import ClassVar

from qgis.core import (
    QgsApplication,
    QgsMessageLog,
    QgsPointXY,
    QgsProcessingFeedback,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelOutput,
    QgsProcessingModelParameter,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDistance,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterPoint,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProject,
)
from qgis.PyQt.QtCore import QCoreApplication, QPointF

from ..compat import (
    LAYER_RASTER,
    MSG_INFO,
    MSG_WARNING,
    PROC_FILE_FOLDER,
    PROC_NUM_INTEGER,
    PROCESSING_OPTIONAL,
)
from ..errors import CommandError
from ..registry import command

# Reported errors reach the caller, so keep only the informative head of them.
_MAX_TRACKED_ERRORS = 10
_MAX_ERROR_LENGTH = 500


class _ResponsiveFeedback(QgsProcessingFeedback):
    """Processing feedback that keeps the GUI alive and enforces a deadline.

    ``processing.run()`` is synchronous and, called straight from the server's
    timer callback, blocks Qt's event loop for its whole duration - the QGIS
    window freezes under the user's cursor, which defeats the point of driving a
    live instance they can also touch. Pumping the event loop on each progress
    report keeps the UI responsive and gives the algorithm a cancellation point.

    Re-entrancy is safe: ``processEvents`` re-fires the server timer, but
    ``_in_dispatch`` makes ``process_server`` return immediately, so no nested
    command runs inside this one.

    Caveat: algorithms that never report progress never reach these hooks, so
    they can neither pump nor time out. Long raster work is better run with
    GDAL outside the live instance.
    """

    # Pump at most this often; processEvents() on every progress tick of a fast
    # algorithm costs more than the responsiveness it buys.
    _PUMP_INTERVAL = 0.05  # seconds

    def __init__(self, budget_seconds):
        super().__init__()
        self.budget = budget_seconds
        self._deadline = time.monotonic() + budget_seconds
        self._last_pump = 0.0
        self.timed_out = False
        self.errors = []

    def _tick(self):
        now = time.monotonic()
        if now >= self._deadline:
            self.timed_out = True
            self.cancel()
            return
        if now - self._last_pump >= self._PUMP_INTERVAL:
            self._last_pump = now
            QCoreApplication.processEvents()

    def setProgress(self, progress):
        self._tick()
        super().setProgress(progress)

    def pushInfo(self, info):
        self._tick()
        super().pushInfo(info)

    def reportError(self, error, fatalError=False):
        # The GDAL providers shell out and surface a non-zero exit code only
        # through here, never through the results dict, so keeping the messages
        # is the only way a failed run can be described to the caller.
        if len(self.errors) < _MAX_TRACKED_ERRORS:
            self.errors.append(str(error)[:_MAX_ERROR_LENGTH])
        super().reportError(error, fatalError)


class ProcessingHandlers:
    """Processing algorithms, models and the analysis commands built on them."""

    # Below the client's TIMEOUT_LONG (60s) so the plugin gives up - and says
    # why - before the client abandons the request and leaves QGIS still working.
    _PROCESSING_TIMEOUT = 55

    # Output values that name no file on disk, so there is nothing to verify.
    _NON_FILE_OUTPUTS: ClassVar[tuple] = (
        "TEMPORARY_OUTPUT",
        "memory:",
        "ogr:",
        "postgres:",
        "postgresql:",
        "mssql:",
    )

    def _run_alg(self, algorithm, parameters, feedback=None):
        """Run *algorithm*, raising when it did not actually produce its output.

        ``processing.run()`` returns an algorithm's declared outputs whether or
        not the run succeeded. The GDAL providers shell out and report a
        non-zero exit code only through the feedback object, so a command that
        wrote nothing still comes back as ``{"OUTPUT": "/path/never/written"}``
        and the caller builds on a file that does not exist. Checking the
        caller's own output paths catches that for every provider without
        parsing anyone's error text, which is translated and provider-specific.
        """
        import processing

        if feedback is None:
            feedback = _ResponsiveFeedback(self._PROCESSING_TIMEOUT)
        result = processing.run(algorithm, parameters, feedback=feedback)
        if feedback.timed_out:
            raise CommandError(
                f"Processing cancelled after {feedback.budget:g}s. Pass a larger 'timeout', "
                "or run heavy raster work with GDAL outside QGIS."
            )
        missing = self._missing_outputs(algorithm, parameters)
        if missing:
            detail = f": {feedback.errors[0]}" if feedback.errors else ""
            raise CommandError(
                f"{algorithm} reported success but wrote no {', '.join(missing)}{detail}"
            )
        return result

    def _missing_outputs(self, algorithm, parameters):
        """Output paths the caller asked for that are not on disk after the run."""
        from qgis.core import QgsProcessingDestinationParameter

        alg = algorithm
        if isinstance(alg, str):
            alg = QgsApplication.processingRegistry().algorithmById(alg)
        if alg is None:
            return []

        missing = []
        for param in alg.parameterDefinitions():
            if not isinstance(param, QgsProcessingDestinationParameter):
                continue
            value = parameters.get(param.name())
            if not isinstance(value, str) or not value:
                continue
            path = value.split("|", 1)[0]
            if "://" in path or path.startswith(self._NON_FILE_OUTPUTS):
                continue
            parent = os.path.dirname(path)
            # A parent that does not exist means this is not a plain filesystem
            # path after all (a provider uri, a layer name), so leave it alone
            # rather than call the run failed on a guess.
            if parent and not os.path.isdir(parent):
                continue
            if not os.path.exists(path):
                missing.append(path)
        return missing

    @command
    def execute_processing(self, algorithm, parameters, timeout=None, **kwargs):
        try:
            QgsMessageLog.logMessage(f"Processing: {algorithm}", self.LOG_TAG, MSG_INFO)
            budget = self._PROCESSING_TIMEOUT if timeout is None else float(timeout)
            feedback = _ResponsiveFeedback(budget)
            result = self._run_alg(algorithm, parameters, feedback)
            response = {"algorithm": algorithm, "result": {k: str(v) for k, v in result.items()}}
            if feedback.errors:
                # The outputs are there, so this is not a failure - but GDAL
                # writes real warnings to stderr and swallowing them is what
                # made a silently failed run so hard to see.
                response["warnings"] = feedback.errors
            return response
        except CommandError:
            # Already a deliberate, user-facing message (the timeout above).
            # Re-wrapping it produced "Processing error: Processing cancelled
            # after 55s...", which reads like the timeout was itself a failure.
            raise
        except Exception as e:
            raise CommandError(f"Processing error: {e!s}") from e

    @command
    def list_processing_algorithms(self, search=None, provider=None, **kwargs):
        registry = QgsApplication.processingRegistry()
        algorithms = []

        for alg in registry.algorithms():
            if provider and alg.provider().id() != provider:
                continue
            if search:
                search_lower = search.lower()
                in_id = search_lower in alg.id().lower()
                in_name = search_lower in alg.displayName().lower()
                if not in_id and not in_name:
                    continue
            algorithms.append(
                {
                    "id": alg.id(),
                    "name": alg.displayName(),
                    "provider": alg.provider().id(),
                }
            )

        return {"algorithms": algorithms, "count": len(algorithms)}

    @command
    def get_algorithm_help(self, algorithm_id, **kwargs):
        registry = QgsApplication.processingRegistry()
        alg = registry.algorithmById(algorithm_id)
        if not alg:
            raise CommandError(f"Algorithm not found: {algorithm_id}")

        params = []
        for param in alg.parameterDefinitions():
            param_info = {
                "name": param.name(),
                "description": param.description(),
                "type": param.type(),
                "optional": bool(param.flags() & PROCESSING_OPTIONAL),
            }
            with contextlib.suppress(Exception):
                default = param.defaultValue()
                if default is not None:
                    param_info["default"] = str(default)
            params.append(param_info)

        outputs = []
        for out in alg.outputDefinitions():
            outputs.append(
                {
                    "name": out.name(),
                    "description": out.description(),
                    "type": out.type(),
                }
            )

        return {
            "id": alg.id(),
            "name": alg.displayName(),
            "description": alg.shortDescription() or "",
            "provider": alg.provider().id(),
            "parameters": params,
            "outputs": outputs,
        }

    def _resolve_algorithm_id(self, hint, registry):
        """Resolve an algorithm hint to a fully-qualified id (e.g. 'native:buffer').

        Direct lookup against ``QgsApplication.processingRegistry()``: the LLM
        passes a keyword like ``"buffer"`` or a full id, and this matches it
        against algorithm ids, display names and tags. Falls back with a
        candidate list when the hint is ambiguous, so the caller can refine.
        """
        if not isinstance(hint, str) or not hint.strip():
            raise CommandError("Algorithm hint must be a non-empty string")
        hint_clean = hint.strip()

        # 1. Exact id match (incl. fully qualified 'native:buffer').
        alg = registry.algorithmById(hint_clean)
        if alg is not None:
            return alg.id()

        hint_lower = hint_clean.lower()
        exact_name = []  # display name == hint
        suffix_id = []  # id suffix == hint (after ':')
        contains = []  # display name or id suffix contains hint
        for alg in registry.algorithms():
            alg_id = alg.id()
            id_suffix = alg_id.split(":", 1)[-1].lower()
            disp = alg.displayName().lower()
            if disp == hint_lower:
                exact_name.append(alg)
            elif id_suffix == hint_lower:
                suffix_id.append(alg)
            elif hint_lower in disp or hint_lower in id_suffix:
                contains.append(alg)

        def _pick(group):
            if len(group) == 1:
                return group[0].id()
            natives = [a for a in group if a.provider().id() == "native"]
            if len(natives) == 1:
                return natives[0].id()
            return None

        for group in (exact_name, suffix_id, contains):
            picked = _pick(group)
            if picked:
                return picked

        all_candidates = exact_name + suffix_id + contains
        if not all_candidates:
            raise CommandError(
                f"No Processing algorithm matches '{hint_clean}'. "
                "Pass a keyword found in the algorithm name or its full id (e.g. 'native:buffer')."
            )
        # Show up to 8 candidates so the LLM can disambiguate next call.
        sample = ", ".join(
            f"{a.id()} ({a.displayName()})"
            for a in sorted(
                all_candidates, key=lambda a: (a.provider().id() != "native", len(a.id()))
            )[:8]
        )
        raise CommandError(
            f"Algorithm hint '{hint_clean}' is ambiguous. Candidates: {sample}. Use the full id."
        )

    def _build_param_source(self, value, defined_inputs, defined_steps):
        """Convert a JSON-friendly value into a QgsProcessingModelChildParameterSource.

        String prefixes:
          @name          -> reference to model input parameter
          $step.OUTPUT   -> reference to a previous step's output
          =expression    -> evaluated QGIS expression
        Lists are converted element-wise; everything else becomes a static value.
        """
        Src = QgsProcessingModelChildParameterSource

        if isinstance(value, list):
            return [self._build_param_source(v, defined_inputs, defined_steps)[0] for v in value]

        if isinstance(value, str):
            if value.startswith("@"):
                ref = value[1:]
                if ref not in defined_inputs:
                    raise CommandError(
                        f"Parameter reference '{value}' points to undefined model input '{ref}'"
                    )
                return [Src.fromModelParameter(ref)]
            if value.startswith("$"):
                rest = value[1:]
                if "." not in rest:
                    raise CommandError(
                        f"Step output reference '{value}' must be in '$step_id.OUTPUT_NAME' form"
                    )
                child_id, output_name = rest.split(".", 1)
                if child_id not in defined_steps:
                    raise CommandError(
                        f"Step output reference '{value}' points to undefined step '{child_id}'"
                    )
                return [Src.fromChildOutput(child_id, output_name)]
            if value.startswith("="):
                return [Src.fromExpression(value[1:])]

        return [Src.fromStaticValue(value)]

    # Spelling variants the tool documents, mapped onto one canonical name each,
    # so the builder tables below carry one row per actual parameter type.
    _INPUT_ALIASES: ClassVar[dict] = {
        "vector_layer": "vector",
        "source": "feature_source",
        "raster_layer": "raster",
        "int": "integer",
        "float": "number",
        "double": "number",
        "bool": "boolean",
        "layers": "multiple_layers",
    }

    # Types built by the common ``Class(name, description, defaultValue=...)``
    # signature. Anything needing more than that gets a builder method below.
    _INPUT_CLASSES: ClassVar[dict] = {
        "vector": QgsProcessingParameterVectorLayer,
        "feature_source": QgsProcessingParameterFeatureSource,
        "raster": QgsProcessingParameterRasterLayer,
        "number": QgsProcessingParameterNumber,
        "string": QgsProcessingParameterString,
        "extent": QgsProcessingParameterExtent,
        "point": QgsProcessingParameterPoint,
        "file": QgsProcessingParameterFile,
        "multiple_layers": QgsProcessingParameterMultipleLayers,
    }

    # Builders are stored by name and resolved with getattr: a staticmethod
    # object is not callable on Python 3.9, which QGIS still bundles.
    _INPUT_BUILDERS: ClassVar[dict] = {
        "field": "_input_field",
        "integer": "_input_integer",
        "distance": "_input_distance",
        "boolean": "_input_boolean",
        "crs": "_input_crs",
        "folder": "_input_folder",
        "enum": "_input_enum",
    }

    @staticmethod
    def _input_field(name, description, default, spec):
        parent = spec.get("parent_layer")
        if not parent:
            raise CommandError(f"Input '{name}' of type 'field' requires 'parent_layer'")
        return QgsProcessingParameterField(
            name, description, parentLayerParameterName=parent, defaultValue=default
        )

    @staticmethod
    def _input_integer(name, description, default, spec):
        param = QgsProcessingParameterNumber(name, description, defaultValue=default)
        with contextlib.suppress(AttributeError):
            param.setDataType(PROC_NUM_INTEGER)
        return param

    @staticmethod
    def _input_distance(name, description, default, spec):
        param = QgsProcessingParameterDistance(name, description, defaultValue=default)
        parent = spec.get("parent_layer")
        if parent:
            param.setParentParameterName(parent)
        return param

    @staticmethod
    def _input_boolean(name, description, default, spec):
        return QgsProcessingParameterBoolean(
            name, description, defaultValue=bool(default) if default is not None else False
        )

    @staticmethod
    def _input_crs(name, description, default, spec):
        return QgsProcessingParameterCrs(name, description, defaultValue=default or "EPSG:4326")

    @staticmethod
    def _input_folder(name, description, default, spec):
        param = QgsProcessingParameterFile(name, description, defaultValue=default)
        with contextlib.suppress(AttributeError):
            param.setBehavior(PROC_FILE_FOLDER)
        return param

    @staticmethod
    def _input_enum(name, description, default, spec):
        return QgsProcessingParameterEnum(
            name, description, options=spec.get("options") or [], defaultValue=default
        )

    def _make_input_definition(self, spec):
        """Build a QgsProcessingParameterDefinition from a JSON spec dict."""
        raw_type = (spec.get("type") or "string").lower()
        type_name = self._INPUT_ALIASES.get(raw_type, raw_type)
        name = spec["name"]
        description = spec.get("description", name)
        default = spec.get("default", None)

        builder = self._INPUT_BUILDERS.get(type_name)
        param_class = self._INPUT_CLASSES.get(type_name)
        if builder is not None:
            param = getattr(self, builder)(name, description, default, spec)
        elif param_class is not None:
            param = param_class(name, description, defaultValue=default)
        else:
            raise CommandError(f"Unsupported input type '{raw_type}' for input '{name}'")

        if spec.get("optional"):
            with contextlib.suppress(Exception):
                param.setFlags(param.flags() | PROCESSING_OPTIONAL)
        return param

    @command
    def create_processing_model(
        self,
        name,
        steps,
        inputs=None,
        outputs=None,
        description="",
        group="Models",
        **kwargs,
    ):
        """Build a Processing Model from a structured spec, save it into the
        QGIS user models folder under a unique name, and register it.

        Reference syntax in step parameter values:
          "@input_name"        – model input parameter
          "$step_id.OUTPUT"    – output of a previous step
          "=expression"        – QGIS expression
          anything else        – static literal (numbers, bools, strings, lists, ...)
        """
        if not name or not isinstance(name, str):
            raise CommandError("Model 'name' is required")
        if not isinstance(steps, list) or not steps:
            raise CommandError("'steps' must be a non-empty list")

        registry = QgsApplication.processingRegistry()

        # ---- Resolve models folder & pick a unique file name up front ----
        provider = registry.providerById("model")
        models_dir = None
        if provider is not None and hasattr(provider, "modelsFolder"):
            try:
                models_dir = provider.modelsFolder()
            except Exception:
                models_dir = None
        if models_dir is None:
            models_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "processing", "models")
        os.makedirs(models_dir, exist_ok=True)

        final_name = name
        target_path = os.path.join(models_dir, f"{final_name}.model3")
        if os.path.exists(target_path):
            for suffix in range(2, 1001):
                candidate = f"{name}_{suffix}"
                candidate_path = os.path.join(models_dir, f"{candidate}.model3")
                if not os.path.exists(candidate_path):
                    final_name = candidate
                    target_path = candidate_path
                    break
            else:
                raise CommandError(
                    f"Could not find a unique name for '{name}' in {models_dir} (tried up to _1000)"
                )

        # ---- Build model skeleton ----
        model = QgsProcessingModelAlgorithm()
        model.setName(final_name)
        if group:
            model.setGroup(group)
        if description:
            with contextlib.suppress(Exception):
                model.setHelpContent({"ALG_DESC": description})

        # ---- Inputs ----
        defined_inputs = set()
        for idx, spec in enumerate(inputs or []):
            if not isinstance(spec, dict) or "name" not in spec:
                raise CommandError(f"Input #{idx} must be a dict with at least 'name'")
            param_def = self._make_input_definition(spec)
            mp = QgsProcessingModelParameter(spec["name"])
            mp.setPosition(QPointF(50.0, 50.0 + idx * 100.0))
            model.addModelParameter(param_def, mp)
            defined_inputs.add(spec["name"])

        # ---- Steps ----
        # Validate shape & resolve algorithm hints up front so we never write
        # a half-built file. Each step entry is normalized to a fully-qualified
        # algorithm id stored under '_resolved_algorithm'.
        defined_steps: list[str] = []
        resolved: list[tuple[dict, str]] = []  # (step_spec, resolved_alg_id)
        seen_ids: set[str] = set()
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise CommandError(f"Step #{idx} must be a dict")
            for required in ("id", "algorithm"):
                if required not in step:
                    raise CommandError(f"Step #{idx} missing required key '{required}'")
            if step["id"] in seen_ids:
                raise CommandError(f"Duplicate step id '{step['id']}'")
            seen_ids.add(step["id"])
            try:
                alg_id = self._resolve_algorithm_id(step["algorithm"], registry)
            except Exception as e:
                raise CommandError(f"Step '{step['id']}': {e}") from e
            alg = registry.algorithmById(alg_id)
            valid_params = {p.name() for p in alg.parameterDefinitions()}
            for pname in step.get("parameters") or {}:
                if pname not in valid_params:
                    raise CommandError(
                        f"Step '{step['id']}' (algorithm '{alg_id}'): unknown parameter "
                        f"'{pname}'. Valid parameters: {sorted(valid_params)}"
                    )
            resolved.append((step, alg_id))

        # Outputs may be marked on a per-step basis; collect them by step id
        outputs_by_step: dict[str, dict[str, dict]] = {}
        step_id_to_alg: dict[str, str] = {step["id"]: alg_id for step, alg_id in resolved}
        for out_idx, out_spec in enumerate(outputs or []):
            if not isinstance(out_spec, dict):
                raise CommandError(f"Output #{out_idx} must be a dict")
            for required in ("name", "from_step", "from_output"):
                if required not in out_spec:
                    raise CommandError(f"Output #{out_idx} missing required key '{required}'")
            from_step = out_spec["from_step"]
            if from_step not in step_id_to_alg:
                raise CommandError(
                    f"Output '{out_spec['name']}': from_step '{from_step}' is not a defined step"
                )
            from_alg = registry.algorithmById(step_id_to_alg[from_step])
            valid_outputs = {o.name() for o in from_alg.outputDefinitions()}
            if out_spec["from_output"] not in valid_outputs:
                raise CommandError(
                    f"Output '{out_spec['name']}': '{out_spec['from_output']}' is not an output "
                    f"of step '{from_step}' (algorithm '{step_id_to_alg[from_step]}'). "
                    f"Valid outputs: {sorted(valid_outputs)}"
                )
            outputs_by_step.setdefault(from_step, {})[out_spec["name"]] = out_spec

        for step_idx, (step, alg_id) in enumerate(resolved):
            child = QgsProcessingModelChildAlgorithm(alg_id)
            child.setChildId(step["id"])
            child.setDescription(
                step.get("description") or registry.algorithmById(alg_id).displayName()
            )
            child.setPosition(QPointF(300.0 + step_idx * 250.0, 50.0))

            for pname, pvalue in (step.get("parameters") or {}).items():
                # Build sources, validating refs against already-defined inputs/steps.
                sources = self._build_param_source(pvalue, defined_inputs, set(defined_steps))
                child.addParameterSources(pname, sources)

            # Final outputs declared for this step
            step_outputs = outputs_by_step.get(step["id"], {})
            if step_outputs:
                model_outputs = {}
                for out_name, out_spec in step_outputs.items():
                    mo = QgsProcessingModelOutput(out_name)
                    mo.setChildId(step["id"])
                    mo.setChildOutputName(out_spec["from_output"])
                    mo.setDescription(out_spec.get("description") or out_name)
                    model_outputs[out_name] = mo
                child.setModelOutputs(model_outputs)

            model.addChildAlgorithm(child)
            defined_steps.append(step["id"])

        # If the user did not declare any outputs, expose the last step's OUTPUT
        # under a default name so the model produces something the user can save.
        if not outputs and defined_steps:
            last_step_id = defined_steps[-1]
            last_child = model.childAlgorithm(last_step_id)
            last_alg = registry.algorithmById(last_child.algorithmId())
            output_names = [o.name() for o in last_alg.outputDefinitions()] if last_alg else []
            preferred = (
                "OUTPUT"
                if "OUTPUT" in output_names
                else (output_names[0] if output_names else None)
            )
            if preferred:
                mo = QgsProcessingModelOutput("Result")
                mo.setChildId(last_step_id)
                mo.setChildOutputName(preferred)
                mo.setDescription("Result")
                last_child.setModelOutputs({"Result": mo})

        # ---- Write the .model3 file directly into the models folder ----
        if not model.toFile(target_path):
            raise CommandError(f"Failed to write model to {target_path}")

        # ---- Register with the model provider so it shows up in the toolbox ----
        registered = False
        if provider is not None:
            try:
                provider.refreshAlgorithms()
                registered = True
            except Exception as e:
                QgsMessageLog.logMessage(
                    f"Model saved but provider refresh failed: {e}", self.LOG_TAG, MSG_WARNING
                )

        QgsMessageLog.logMessage(
            f"Processing model '{final_name}' saved to {target_path}", self.LOG_TAG, MSG_INFO
        )
        output_count = sum(len(v) for v in outputs_by_step.values()) or (1 if defined_steps else 0)
        return {
            "ok": True,
            "name": final_name,
            "requested_name": name,
            "path": target_path,
            "registered": registered,
            "input_count": len(defined_inputs),
            "step_count": len(defined_steps),
            "output_count": output_count,
            # Echo the resolved algorithm ids so the caller can confirm fuzzy matches.
            "resolved_steps": [
                {"id": step["id"], "algorithm": alg_id, "hint": step["algorithm"]}
                for step, alg_id in resolved
            ],
        }

    @command
    def list_processing_models(self, **kwargs):
        """List registered Processing models (provider 'model')."""
        registry = QgsApplication.processingRegistry()
        models = []
        for alg in registry.algorithms():
            if alg.provider().id() == "model":
                models.append({"id": alg.id(), "name": alg.displayName(), "group": alg.group()})
        return {"models": models, "count": len(models)}

    @command
    def run_model(self, model, parameters=None, **kwargs):
        """Run a Processing model by registered id or by .model3 file path."""
        from qgis.core import QgsProcessingDestinationParameter

        parameters = dict(parameters or {})
        if isinstance(model, str) and model.lower().endswith(".model3"):
            alg = QgsProcessingModelAlgorithm()
            if not alg.fromFile(model):
                raise CommandError(f"Failed to load model file: {model}")
            alg.initAlgorithm()
            target = alg
        else:
            registry = QgsApplication.processingRegistry()
            alg = registry.algorithmById(model)
            # Registered model ids carry a "model:" prefix, but callers see the
            # bare name in create_processing_model's response - accept both.
            if alg is None and isinstance(model, str) and not model.startswith("model:"):
                prefixed = f"model:{model}"
                alg = registry.algorithmById(prefixed)
                if alg is not None:
                    model = prefixed
            target = model

        # Destination (sink/output) parameters have no default, so omitting one
        # aborts the run. The Processing GUI defaults them to a temporary layer;
        # do the same so callers only have to supply the model's real inputs.
        if alg is not None:
            for param in alg.parameterDefinitions():
                if isinstance(param, QgsProcessingDestinationParameter):
                    parameters.setdefault(param.name(), "TEMPORARY_OUTPUT")

        result = self._run_alg(target, parameters)
        return {"model": model, "result": {k: str(v) for k, v in result.items()}}

    @command
    def get_processing_providers(self, **kwargs):
        """List Processing providers with algorithm counts and active status."""
        registry = QgsApplication.processingRegistry()
        providers = []
        for p in registry.providers():
            info = {
                "id": p.id(),
                "name": p.name(),
                "algorithm_count": len(p.algorithms()),
            }
            with contextlib.suppress(Exception):
                info["active"] = bool(p.isActive())
            providers.append(info)
        return {"providers": providers, "count": len(providers)}

    @command
    def execute_processing_batch(self, algorithm, parameters_list, timeout=None, **kwargs):
        """Run the same algorithm once per parameter dict; collect per-run results.

        `timeout` bounds the whole batch (default `_PROCESSING_TIMEOUT`), not each run:
        runs that would start after it has elapsed are reported as skipped, so the caller
        gets the results collected so far instead of a client timeout that loses all of
        them (#43).
        """
        budget = self._PROCESSING_TIMEOUT if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        results = []
        for i, params in enumerate(parameters_list):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(
                    {
                        "index": i,
                        "status": "skipped",
                        "message": f"Batch budget of {budget:g}s exhausted before this run started",
                    }
                )
                continue
            try:
                feedback = _ResponsiveFeedback(min(self._PROCESSING_TIMEOUT, remaining))
                r = self._run_alg(algorithm, params, feedback)
                results.append(
                    {
                        "index": i,
                        "status": "success",
                        "result": {k: str(v) for k, v in r.items()},
                    }
                )
            except Exception as e:
                results.append({"index": i, "status": "error", "message": str(e)})
        response = {"algorithm": algorithm, "results": results, "count": len(results)}
        if any(r["status"] == "skipped" for r in results):
            response["timed_out"] = True
        return response

    @command
    def raster_calculator(self, expression, output_path, reference_layer=None, **kwargs):
        """Band math via QgsRasterCalculator. Reference loaded rasters as 'name@band'."""
        from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry

        project = QgsProject.instance()
        entries = []
        ref = None
        rasters = []
        for lid, layer in project.mapLayers().items():
            if layer.type() != LAYER_RASTER:
                continue
            rasters.append(layer)
            for band in range(1, layer.bandCount() + 1):
                e = QgsRasterCalculatorEntry()
                e.ref = f"{layer.name()}@{band}"
                e.raster = layer
                e.bandNumber = band
                entries.append(e)
            if reference_layer and reference_layer in (lid, layer.name()):
                ref = layer
        if ref is None:
            if not rasters:
                raise CommandError("No raster layers loaded to compute from")
            ref = rasters[0]

        extent = ref.extent()
        cols = ref.width()
        rows = ref.height()
        try:
            calc = QgsRasterCalculator(
                expression,
                output_path,
                "GTiff",
                extent,
                cols,
                rows,
                entries,
                project.transformContext(),
            )
        except TypeError:
            calc = QgsRasterCalculator(
                expression, output_path, "GTiff", extent, cols, rows, entries
            )
        res = calc.processCalculation()
        if int(res) != 0:
            raise CommandError(f"Raster calculation failed (code {int(res)})")
        return {"ok": True, "output": output_path, "reference_layer": ref.name()}

    @command
    def zonal_statistics(
        self,
        polygon_layer,
        raster_layer,
        band=1,
        prefix="_",
        stats=None,
        output_path=None,
        **kwargs,
    ):
        """Per-polygon raster statistics (native:zonalstatisticsfb).

        stats: list of int codes (0=count,1=sum,2=mean,3=median,4=stdev,5=min,
        6=max,7=range,8=minority,9=majority,10=variety,11=variance).
        """
        poly = self._get_vector_layer(polygon_layer)
        rast = self._get_raster_layer(raster_layer)
        params = {
            "INPUT": poly,
            "INPUT_RASTER": rast,
            "RASTER_BAND": band,
            "COLUMN_PREFIX": prefix,
            "STATISTICS": stats or [0, 1, 2],
            "OUTPUT": output_path or "memory:zonal_stats",
        }
        r = self._run_alg("native:zonalstatisticsfb", params)
        return self._register_output(r["OUTPUT"], "zonal_stats")

    @command
    def sample_raster_values(self, raster_layer, points, band=None, **kwargs):
        """Sample raster values at points [[x, y], ...] in the raster's CRS."""
        layer = self._get_raster_layer(raster_layer)
        dp = layer.dataProvider()
        results = []
        for pt in points:
            p = QgsPointXY(pt[0], pt[1])
            if band:
                val, ok = dp.sample(p, band)
                results.append({"x": pt[0], "y": pt[1], "band": band, "value": val if ok else None})
            else:
                vals = {}
                for b in range(1, layer.bandCount() + 1):
                    v, ok = dp.sample(p, b)
                    vals[b] = v if ok else None
                results.append({"x": pt[0], "y": pt[1], "values": vals})
        return {"samples": results, "count": len(results)}

    @command
    def spatial_join(
        self,
        target_layer,
        join_layer,
        predicates=None,
        join_fields=None,
        method=1,
        prefix="",
        output_path=None,
        **kwargs,
    ):
        """Join attributes by location (native:joinattributesbylocation).

        predicates: list of int (0=intersects,1=contains,2=equals,3=touches,
        4=overlaps,5=within,6=crosses). method: 0=one-to-many, 1=first match,
        2=largest overlap.
        """
        target = self._get_vector_layer(target_layer)
        join = self._get_vector_layer(join_layer)
        params = {
            "INPUT": target,
            "JOIN": join,
            "PREDICATE": predicates or [0],
            "JOIN_FIELDS": join_fields or [],
            "METHOD": method,
            "PREFIX": prefix,
            "OUTPUT": output_path or "memory:joined",
        }
        r = self._run_alg("native:joinattributesbylocation", params)
        return self._register_output(r["OUTPUT"], "joined")

    def _register_output(self, out, default_name):
        """Add a processing output layer to the project, or report a file path."""
        if isinstance(out, str):
            return {"output": out}
        out.setName(default_name)
        QgsProject.instance().addMapLayer(out)
        return {"output_layer_id": out.id(), "name": out.name()}
