"""AWS Elemental MediaConnect Router input content-quality monitor.

Amazon EventBridge invokes this function when a Router input reports a frozen-frame
content-quality detection. The function changes the preferred failover source from
the primary source to the secondary source, captures a thumbnail and source metadata
in Amazon S3, and publishes an Amazon SNS notification containing the stored S3 paths.

This code is provided for demonstration and educational purposes. Review, test, and
harden it against your own requirements before relying on it in any environment.
"""
import base64
import copy
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
sns = boto3.client("sns")
mediaconnect = boto3.client("mediaconnect")


class MediaConnectRouterMonitor:
    """Handles failover, evidence capture, and notification for one Router input."""

    def __init__(self):
        self.router_input_arn = os.environ["ROUTER_INPUT_ARN"]
        self.bucket = os.environ["S3_BUCKET"]
        self.topic_arn = os.environ["SNS_TOPIC_ARN"]
        self.primary_index = self._read_primary_index()
        # Two-source PRIMARY_SECONDARY config: the secondary is the other index.
        self.secondary_index = 1 - self.primary_index

    @staticmethod
    def _read_primary_index():
        """Return the primary source index (0 or 1), or raise on a bad value."""
        raw_value = os.environ.get("PRIMARY_SOURCE_INDEX", "0")
        try:
            primary_index = int(raw_value)
        except ValueError:
            raise ValueError(
                "PRIMARY_SOURCE_INDEX must be the integer 0 or 1; "
                f"got {raw_value!r}"
            )
        if primary_index not in (0, 1):
            raise ValueError(
                f"PRIMARY_SOURCE_INDEX must be 0 or 1; got {primary_index}"
            )
        return primary_index

    def get_failover_configuration(self):
        """Return a copy of the current failover configuration, or None on error."""
        try:
            router_input = mediaconnect.get_router_input(
                Arn=self.router_input_arn
            ).get("RouterInput", {})
            configuration = router_input.get("Configuration", {}).get("Failover")
            if not configuration:
                logger.error("Router input does not have a failover configuration")
                return None
            if configuration.get("SourcePriorityMode") != "PRIMARY_SECONDARY":
                logger.error(
                    "Router input source priority mode is %s, not PRIMARY_SECONDARY",
                    configuration.get("SourcePriorityMode"),
                )
                return None
            return copy.deepcopy(configuration)
        except Exception as exc:
            logger.error("Failed to get Router input: %s", exc)
            return None

    def switch_to_secondary(self, failover_configuration):
        """Set the preferred source index to the secondary source."""
        writable_fields = (
            "NetworkInterfaceArn",
            "ProtocolConfigurations",
            "SourcePriorityMode",
        )
        updated_configuration = {
            field: copy.deepcopy(failover_configuration[field])
            for field in writable_fields
            if field in failover_configuration
        }
        updated_configuration["PrimarySourceIndex"] = self.secondary_index
        try:
            mediaconnect.update_router_input(
                Arn=self.router_input_arn,
                Configuration={"Failover": updated_configuration},
            )
            return True
        except Exception as exc:
            logger.error("Failed to switch the preferred Router input source: %s", exc)
            return False

    def get_thumbnail(self):
        """Return the current Router input thumbnail as bytes, or None."""
        try:
            response = mediaconnect.get_router_input_thumbnail(
                Arn=self.router_input_arn
            )
            thumbnail = response.get("ThumbnailDetails", {}).get("Thumbnail")
            if isinstance(thumbnail, str):
                return base64.b64decode(thumbnail)
            return bytes(thumbnail) if thumbnail else None
        except Exception as exc:
            logger.error("Failed to get Router input thumbnail: %s", exc)
            return None

    def save_thumbnail(self, thumbnail, key):
        """Store thumbnail bytes in S3. Return the S3 URI on success."""
        if not thumbnail:
            logger.warning("No thumbnail data to save")
            return None
        try:
            s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=thumbnail,
                ContentType="image/jpeg",
            )
            return f"s3://{self.bucket}/{key}"
        except Exception as exc:
            logger.error("Failed to save thumbnail to S3: %s", exc)
            return None

    def get_source_metadata(self):
        """Fetch and log Router input source metadata, or return None."""
        try:
            response = mediaconnect.get_router_input_source_metadata(
                Arn=self.router_input_arn
            )
        except Exception as exc:
            logger.error("Failed to get Router input source metadata: %s", exc)
            return None

        details = response.get("SourceMetadataDetails", {})
        messages = details.get("SourceMetadataMessages", [])
        metadata = details.get("RouterInputMetadata", {})
        if messages:
            logger.warning(
                "Router input source metadata messages: %s",
                json.dumps(messages, default=str),
            )
        logger.info(
            "Router input source metadata: %s", json.dumps(metadata, default=str)
        )

        return {
            "routerInputArn": response.get("Arn", self.router_input_arn),
            "routerInputName": response.get("Name"),
            "timestamp": details.get("Timestamp"),
            "messages": messages,
            "routerInputMetadata": metadata,
        }

    def save_metadata(self, metadata, key):
        """Store source metadata as JSON in S3. Return the S3 URI on success."""
        if not metadata:
            logger.warning("No source metadata to save")
            return None
        try:
            s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(metadata, default=str, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
            return f"s3://{self.bucket}/{key}"
        except Exception as exc:
            logger.error("Failed to save source metadata to S3: %s", exc)
            return None

    def capture_evidence(self):
        """Capture a thumbnail and metadata under one timestamped S3 key prefix.

        Evidence capture is best-effort. Failures are logged and never reverse or
        block a successful failover.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        base_key = f"frozen_frame_{timestamp}_{uuid.uuid4().hex[:8]}"

        thumbnail_uri = self.save_thumbnail(
            self.get_thumbnail(), f"{base_key}.jpg"
        )
        if thumbnail_uri:
            logger.info("Thumbnail saved to %s", thumbnail_uri)
        else:
            logger.warning("Thumbnail capture failed; no thumbnail stored")

        metadata_uri = self.save_metadata(
            self.get_source_metadata(), f"{base_key}.json"
        )
        if metadata_uri:
            logger.info("Source metadata saved to %s", metadata_uri)
        else:
            logger.warning("Source metadata capture failed; no metadata stored")

        return {
            "thumbnail": thumbnail_uri,
            "metadata": metadata_uri,
        }

    def notify_failover(self, evidence):
        """Publish a best-effort SNS notification with the stored S3 paths."""
        stored_paths = [uri for uri in evidence.values() if uri]
        evidence_text = "\n".join(f"- {uri}" for uri in stored_paths)
        if not evidence_text:
            evidence_text = "- No evidence objects were stored; check CloudWatch Logs."

        message = (
            "AWS Elemental MediaConnect detected frozen frames and changed the "
            "Router input's preferred source.\n\n"
            f"Router input: {self.router_input_arn}\n"
            f"Previous preferred source index: {self.primary_index}\n"
            f"Current preferred source index: {self.secondary_index}\n\n"
            "Captured evidence:\n"
            f"{evidence_text}\n"
        )
        try:
            sns.publish(
                TopicArn=self.topic_arn,
                Subject="MediaConnect Router input failover",
                Message=message,
            )
            logger.info("Published failover notification to %s", self.topic_arn)
            return True
        except Exception as exc:
            logger.error("Failed to publish failover notification: %s", exc)
            return False

    def handle_frozen_frames(self):
        """Fail over only when the primary source is currently preferred."""
        configuration = self.get_failover_configuration()
        if configuration is None:
            logger.error("Could not determine the preferred source; taking no action")
            return

        current_index = configuration.get("PrimarySourceIndex")
        if current_index != self.primary_index:
            logger.warning(
                "Frozen frames detected while source index %s is preferred; no "
                "healthy failover target is configured. Manual intervention may "
                "be required",
                current_index,
            )
            self.capture_evidence()
            return

        switched = self.switch_to_secondary(configuration)
        if switched:
            logger.info(
                "Changed preferred source index from %s to %s",
                self.primary_index,
                self.secondary_index,
            )
        else:
            logger.error("Failed to change the preferred source index")

        evidence = self.capture_evidence()
        if switched:
            self.notify_failover(evidence)


def _frozen_frames_detected(detail):
    """Return True when the Router input event reports frozen frames."""
    return detail.get("current", {}).get("frozenFrames") == "DETECTED"


def lambda_handler(event, context):
    """EventBridge entry point; act only on Router frozen-frame detections."""
    try:
        logger.info("Received event: %s", json.dumps(event, default=str))
        if _frozen_frames_detected(event.get("detail", {})):
            MediaConnectRouterMonitor().handle_frozen_frames()
            return {"statusCode": 200, "body": json.dumps("Event processed")}
        logger.info("Ignoring event without a frozen-frame detection")
        return {"statusCode": 200, "body": json.dumps("Event ignored")}
    except Exception as exc:
        logger.exception("Error processing event: %s", exc)
        return {"statusCode": 500, "body": json.dumps("Error processing event")}
