# DiscoverX

Scan, Classify, and Discover the content of your Lakehouse

## Requirements

* A [Databricks workspace](https://www.databricks.com/try-databricks#account)
* [Unity Catalog](https://www.databricks.com/product/unity-catalog)

## Getting started

Install DiscoverX, in Databricks notebook type

```
%pip install discoverx
```

Get started

```
from discoverx import DX
dx = DX()
```

## Scan & classify

You can now scan the content of any set of tables for
- IP addresses (v4 and v6)
- Email addresses
- URLs
- ... and many more

See the full list of rules with 

```
dx.display_rules()
```

You can also provide your [custom matching rules](#custom-rules).

The scan will automatically classify columns where the number of matching records exceeds a [classification threshold](#classification-threshold) (95% by default).


### Example

Scan all (samples 10k rows from each table)

```
dx.scan(from_tables="*.*.*")
```

Check out the [scan parameters](#scan-parameters).

The result is a dataset with a `frequency` column, which defines the fraction of matched records against the total records scanned for each rule.

## Inspect

You can inspect and fine-tune the classification results with 

```
dx.inspect()
```


## Publish the classificaiton

After a `scan` you can publish the classificaiton results in a delta table (by default the table is `_discoverx.classification.tags`).

You can either publish the classification from the inspection UI, or apply directly without fine-tuning by executing

```
dx.publish()
```

## Cross-table queries

After a `publish` you can leverage the classified column tags to run cross-table `search`, `delete` and `select` actions.


### Search

Search for a specific value across multiple tables.

Eg. Search for all rows with `example_email@databricks.com` in a column classified as `dx_email`.

```
dx.search("example_email@databricks.com", from_tables="*.*.*", by_tags=["dx_email"])
```

### Delete

Delete 

Preview delete statements
```
dx.delete_by_tag(from_tables="*.*.*", by_tag="dx_email", values=['example_email@databricks.com'], yes_i_am_sure=False)
```

Execute delete statements
```
dx.delete_by_tag(from_tables="*.*.*", by_tag="dx_email", values=['example_email@databricks.com'], yes_i_am_sure=True)
```

Note: You need to regularely [vacuum](https://docs.delta.io/latest/delta-utility.html#remove-files-no-longer-referenced-by-a-delta-table) all your delta tables to remove all traces of your deleted rows. 

### Select

Select all columns tagged with specified tags from multiple tables

```
dx.select_by_tags(from_tables="*.*.*", by_tags=["dx_iso_date", "dx_email"])
```

You can apply further transformations to build your summary tables. 
Eg. Count the occurrence of each IP address per day across multiple tables and columns

```
df = (dx.select_by_tags(from_tables="*.*.*", by_tags=["dx_iso_date", "dx_ip_v4"])
    .groupby(["catalog", "database", "table", "tagged_columns.dx_iso_date.column", "tagged_columns.dx_iso_date.value", "tagged_columns.dx_ip_v4.column"])
    .agg(func.count("tagged_columns.dx_ip_v4.value").alias("count"))
)
```


## Configuration

### Scan parameters

You can define 

```
dx.scan(
    catalogs="*",      # Catalog filter ('*' is a wildcard)
    databases="*",     # Database filter ('*' is a wildcard)
    tables="*",        # Table filter ('*' is a wildcard)
    rules="*",         # Rule filter ('*' is a wildcard) or list[string]
    sample_size=10000, # Number of rows to sample, use None for a full table scan
    what_if=False      # If `True` it prints the SQL that would be executed
)
```

### Custom rules

You can provide your custom scanning rules based on regex expressions.

```
custom_rules = [
    {
        'name': 'resource_request_id',
        'type': 'regex',
        'description': 'Resource request ID',
        'definition': r'^AR-\d{9}$',
        'match_example': ['AR-123456789'],
        'nomatch_example': ['CD-123456789']
    }
]
dx = DX(custom_rules=custom_rules)
```

You should now see your rules added to the default ones with

```
dx.display_rules()
```

### Classification threshold

You can customize the classification threshold with

```
dx = DX(classification_threshold=0.99)
```

## Project Support
Please note that all projects in the /databrickslabs github account are provided for your exploration only, and are not formally supported by Databricks with Service Level Agreements (SLAs).  They are provided AS-IS and we do not make any guarantees of any kind.  Please do not submit a support ticket relating to any issues arising from the use of these projects.

Any issues discovered through the use of this project should be filed as GitHub Issues on the Repo.  They will be reviewed as time permits, but there are no formal SLAs for support.

