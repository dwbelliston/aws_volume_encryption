# AWS EC2 Root Volume Encryption

## _[v2.1](https://github.com/dwbelliston/aws_volume_encryption/releases/tag/2.1) includes support for tags_

## _[v2.0](https://github.com/dwbelliston/aws_volume_encryption/releases/tag/2.0) includes support for mutli-volume encryption_

## _ReadMe Tutorial is for [v1.0](https://github.com/dwbelliston/aws_volume_encryption/releases/tag/1.0)_

Despite all the planning and preparing we do to architect flawless systems,
we may eventually run into issues with our design. Changing business needs will
mean you need to quickly reassess your design and find reliable solutions.

For example, say you spin up several EC2 instances with unencrypted root
volumes, thinking you would not need to store any sensitive data. Requirements
change and you now need to [encrypt those volumes](http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EBSEncryption.html).
This post will walk through the steps to [encrypt a root volume](https://aws.amazon.com/blogs/aws/new-encrypted-ebs-boot-volumes/)
for an EC2 instance.

## AWS Python SDK

Amazon provides SDKs for many different languages. This example is written
in Python and uses Amazon's Python SDK, [Boto 3](https://github.com/boto/boto3).

Follow the Boto 3 [QuickStart Guide](https://github.com/boto/boto3#quick-start)
to get started with Boto 3.

## AWS CLI Configuration

1.  Install the [AWS CLI](http://docs.aws.amazon.com/cli/latest/userguide/installing.html)
2.  [Configure your client](http://docs.aws.amazon.com/cli/latest/userguide/cli-chap-getting-started.html)

These instructions will walk you through setting up your AWS credentials.

When configuring the AWS CLI it will set up your default profile.
You can also set up multiple ['Named Profiles'](http://docs.aws.amazon.com/cli/latest/userguide/cli-chap-getting-started.html#cli-multiple-profiles)
with different settings. Simply set the `--profile` flag and it will prompt you
for input.

```text
aws configure --profile profilename
```

Your config will be written to a 'config' file in the `$HOME/.aws` directory.
Your credentials will be written to a 'credentials' file in the same directory.

On Windows, these files are located in the `$env:USERPROFILE\.aws` directory.

## IAM Permissions

These IAM policy actions are required in order to run this script.
This is an example policy you can attach to a user to give them the proper IAM
authorization.

```json
{
    "Version": "2010-10-10",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeVolumes",
                "ec2:DescribeSnapshots",
                "ec2:StopInstances",
                "ec2:StartInstances",
                "ec2:CopySnapshot",
                "ec2:CreateSnapshot",
                "ec2:CreateVolume",
                "ec2:DeleteVolume",
                "ec2:DeleteSnapshot",
                "ec2:AttachVolume",
                "ec2:DetachVolume"
            ],
            "Resource": "*"
        }
    ]
}
```

## The Script

First we will layout the overview of the script including parameters it can
receive.

```python
#!/usr/bin/env python

"""
Overview:
    Take unencrypted root volume and encrypt it for EC2.
Params:
    ID for EC2 instance
    Customer Master Key (CMK) (optional)
    Profile to use
Conditions:
    Return if volume already encrypted
    Use named profiles from credentials file
"""
```

This script will use the ID parameter to find the EC2 instance. If the root
volume is already encrypted the script will exit.

Also, we want to take advantage of profiles so we will receive the profile the
user wants to use. The Customer Master Key will be used for the encryption,
but is not required. This will be explained later.

Let's import what we need for the script and set up our argument parser.

```python
import sys
import boto3
import botocore
import argparse


def main(argv):
    parser = argparse.ArgumentParser(description='Encrypts EC2 root volume.')
    parser.add_argument('-i', '--instance',
                        help='Instance to encrypt volume on.', required=True)
    parser.add_argument('-key', '--customer_master_key',
                        help='Customer master key', required=False)
    parser.add_argument('-p', '--profile',
                        help='Profile to use', required=False)
    args = parser.parse_args()
```

### Setting up our session

The next step is to get our session set up. Boto will use the `default` profile
unless the user passed in a `--profile` parameter.

```python
""" Set up AWS Session + Client + Resources + Waiters """
if args.profile:
    # Create custom session
    print('Using profile {}'.format(args.profile))
    session = boto3.session.Session(profile_name=args.profile)
else:
    # Use default session
    session = boto3.session.Session()
```

Two key features of Boto3 are high-level object-oriented resources and low-level
service connections. Low-level services map closely to the AWS service APIs
whereas the high-level resource are abstracted from them.

Another feature are waiters, these are helpful for blocking until desired states
are reached. Two waiters are stored, one for waiting until a snapshot is
completed and the other for waiting until a volume is available.
We get these waiters from the low-level client.

```python
client = session.client('ec2')
ec2 = session.resource('ec2')

waiter_instance_exists = client.get_waiter('instance_exists')
waiter_instance_stopped = client.get_waiter('instance_stopped')
waiter_instance_running = client.get_waiter('instance_running')
waiter_snapshot_complete = client.get_waiter('snapshot_completed')
waiter_volume_available = client.get_waiter('volume_available')
```

If the optional CMK was passed in, we will store it.

```python
# Get CMK
customer_master_key = args.customer_master_key
```

### Check instance state

Before we proceed too far into the script we will make sure that the instance id
we received can be used to retrieve an instance. If its not, we will exit the
script.

```python
""" Check instance exists """
instance_id = args.instance
print('---Checking instance ({})'.format(instance_id))
instance = ec2.Instance(instance_id)

try:
    waiter_instance_exists.wait(
        InstanceIds=[
            instance_id,
        ]
    )
except botocore.exceptions.WaiterError as e:
    sys.exit('ERROR: {}'.format(e))
```

### Steps

We have an instance and the resources we need, let's continue the script and
encrypt the volume.

#### Check for existing encryption

You can access the volumes of the instances. These return a Boto3 'Collection'
which is an iterable of resources. We can iterate through it to get access to
the actual instance of the volume. Note, if the volume is encrypted,
we will exit our script.

```python
""" Get volume and exit if already encrypted """
volumes = [v for v in instance.volumes.all()]
if volumes:
    original_root_volume = volumes[0]
    volume_encrypted = original_root_volume.encrypted
    if volume_encrypted:
        sys.exit(
            '**Volume ({}) is already encrypted'
            .format(original_root_volume.id))
```

#### 1. Shut down if running

The instance volume has mappings that we will want to preserve for the encrypted volume. We are able to modify these. You can do this as needed with other mappings. For this example, we are storing the 'DeleteOnTermination' information, which we will use at the end of the script to make sure it's the same.

```python
""" Step 1: Prepare instance """
print('---Preparing instance')
# Save original mappings to persist to new volume
original_mappings = {}
original_mappings['DeleteOnTermination'] = instance.block_device_mappings[0]['Ebs']['DeleteOnTermination']
```

We won't be able to work with this instance how we want if its running. You can
check the status of instance through its state property. The different
codes are:

```text
- 0 : pending
- 16 : running
- 32 : shutting-down
- 48 : terminated
- 64 : stopping
- 80 : stopped
```

In this case we will exit if the state is pending, shutting-down, or terminated and stop the instance if it is running.

```python
# Exit if instance is pending, shutting-down, or terminated
instance_exit_states = [0, 32, 48]
if instance.state['Code'] in instance_exit_states:
    sys.exit(
        'ERROR: Instance is {} please make sure this instance is active.'
        .format(instance.state['Name'])
    )

# Validate successful shutdown if it is running or stopping
if instance.state['Code'] is 16:
    instance.stop()
```

Now that the signal to stop the instance is sent, we want to wait for the instance to be in its proper state. We can use a waiter, which polls the state of the instance at intervals, to block the code until the instance is stopped.

```python    
try:
    waiter_instance_stopped.wait(
        InstanceIds=[
            instance_id,
        ]
    )
except botocore.exceptions.WaiterError as e:
    sys.exit('ERROR: {}'.format(e))
```

Waiters can be configured to behave as you need, for example, the waiter will poll 40 times to check the state. If it still has not reached the desired state the waiter is looking for it will exit with a 'WaiterError'. You can change this through '.config'.

```python
# Set the max_attempts for this waiter (default 40)
waiter_instance_stopped.config.max_attempts = 40
```

#### 2. Take snapshot

Now that we have the id for the volume we will use that to create a snapshot
of its current state. Immediately after we will use the waiter we stored earlier
to wait for the snapshot to be complete. You can pass multiple ids into the
waiter. In this case, we will just wait for the one we created to be complete.

```python
""" Step 2: Take snapshot of volume """
  print('---Create snapshot of volume ({})'.format(original_root_volume.id))
  snapshot = ec2.create_snapshot(
      VolumeId=original_root_volume.id,
      Description='Snapshot of volume ({})'.format(original_root_volume.id),
  )

  try:
      waiter_snapshot_complete.wait(
          SnapshotIds=[
              snapshot.id,
          ]
      )
  except botocore.exceptions.WaiterError as e:
      # Clean up the snapshot to reduce clutter (optional)
      snapshot.delete()
      sys.exit('ERROR: {}'.format(e))
```

#### 3. Create new encrypted volume

To create the encrypted volumes we can simply create a copy of the snapshot of
the unencrypted volume and set the 'Encrypted' flag to true. In addition to the
encrypted flag, we can set other parameters for this action. If the user passed
their customer master key, meaning they don't want to use the Amazon's default
key management system, we will use that specific key for the encryption. Again,
once the snapshot begins to be copied, we will wait for the snapshot to be
complete before proceeding.

```python
""" Step 3: Create encrypted volume """
  print('---Create encrypted copy of snapshot')
  if customer_master_key:
      # Use custom key
      snapshot_encrypted_dict = snapshot.copy(
          SourceRegion=session.region_name,
          Description='Encrypted copy of snapshot #{}'
                      .format(snapshot.id),
          KmsKeyId=customer_master_key,
          Encrypted=True,
      )
  else:
      # Use default key
      snapshot_encrypted_dict = snapshot.copy(
          SourceRegion=session.region_name,
          Description='Encrypted copy of snapshot ({})'
                      .format(snapshot.id),
          Encrypted=True,
      )

  snapshot_encrypted = ec2.Snapshot(snapshot_encrypted_dict['SnapshotId'])

  try:
      waiter_snapshot_complete.wait(
          SnapshotIds=[
              snapshot_encrypted.id,
          ],
      )
  except botocore.exceptions.WaiterError as e:
      snapshot.delete()
      snapshot_encrypted.delete()
      sys.exit('ERROR: {}'.format(e))
```

The snapshot is complete so we can now take that and create an encrypted volume.
Because the snapshot is encrypted, the volume will be too.

```python
print('---Create encrypted volume from snapshot')
volume_encrypted = ec2.create_volume(
    SnapshotId=snapshot_encrypted.id,
    AvailabilityZone=instance.placement['AvailabilityZone']
)
```

#### 4. Detach current root volume

Before we can attach the new encrypted volume we need to detach the old volume.

```python
""" Step 4: Detach current root volume """
print('---Deatch volume {}'.format(original_root_volume.id))
instance.detach_volume(
    VolumeId=original_root_volume.id,
    Device=instance.root_device_name,
)
```

#### 5. Attach new volume

Once the encrypted volume is complete, we are ready to attach it. Pass in the
volume id and the keep the device type constant by pulling it from the original
instances property.

```python
""" Step 5: Attach current root volume """
  print('---Attach volume {}'.format(volume_encrypted.id))
  try:
      waiter_volume_available.wait(
          VolumeIds=[
              volume_encrypted.id,
          ],
      )
  except botocore.exceptions.WaiterError as e:
      snapshot.delete()
      snapshot_encrypted.delete()
      volume_encrypted.delete()
      sys.exit('ERROR: {}'.format(e))

  instance.attach_volume(
      VolumeId=volume_encrypted.id,
      Device=instance.root_device_name
  )
```

#### 6. Restart instance

The volume is attached so we want to bring our instance back up. We will make sure the original mappings remain. We can access those through the 'modify_attribute' and selecting the root device we just created.

```python
""" Step 6: Restart instance """
# Modify instance attributes
instance.modify_attribute(
    BlockDeviceMappings=[
        {
            'DeviceName': instance.root_device_name,
            'Ebs': {
                'DeleteOnTermination':
                original_mappings['DeleteOnTermination'],
            },
        },
    ],
)
```
We will start the instance and then wait for it to be running.

```python
print('---Restart instance')
instance.start()

try:
    waiter_instance_running.wait(
        InstanceIds=[
            instance_id,
        ]
    )
except botocore.exceptions.WaiterError as e:
    sys.exit('ERROR: {}'.format(e))
```

#### Clean up

We no longer need the snapshot because we have extracted our volume already.
You can do clean up as needed. We can also delete the snapshot_encrypted resource and the original root volume, we don't want that hanging around unencrypted.

```python
""" Step 7: Clean up """
print('---Clean up resources')
# Delete snapshots and original volume
snapshot.delete()
snapshot_encrypted.delete()
original_root_volume.delete()
```

## Summary

In this post you saw how to encrypt the root volume of an existing EC2 instance.
After installing the AWS CLI and the Boto 3 Python SDK, we showed you how to
create a short Python script to snapshot your existing root volume to a new
encrypted root volume and restart your instance. To ensure your data is safe,
the script deletes the original unencrypted volume as the last step.
